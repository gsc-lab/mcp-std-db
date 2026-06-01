"""
Stage 3 — Prompt 를 실행하는 REPL (사용자가 고르는 작업 흐름)

MCP primitive 세 가지 중 마지막은 Prompt 다. 누가 호출을 결정하느냐로 구분하면:
  - Tool     (01~03) = 모델이 turn 중에 호출        (model-controlled)
  - Resource (04)     = 사용자가 /attach 로 컨텍스트 주입 (app/user-controlled)
  - Prompt   (이 파일)= 사용자가 *고르는* 재사용 템플릿   (user-controlled)

Prompt 는 Claude Desktop/Code 에서 슬래시 명령으로 보이는 재사용 작업 흐름이다.
사용자가 이름과 인자를 넘기면 서버가 미리 준비한 메시지 목록을 돌려준다.
클라이언트는 그 메시지를 대화(messages)에 넣고, 평소처럼 multi-turn 루프를 실행한다.
이 예제에서는 /prompt <name> <인자...> 명령으로 Prompt 를 실행한다.

서버의 두 가지 Prompt 패턴 (server/main.py 참고):
  (A) EmbeddedResource 포함 — analyze_student_risk, course_catalog
      서버가 Resource(students://, courses://)를 미리 읽어 메시지에 포함해 보낸다.
      LLM 은 추가 호출 없이 데이터를 받아 분석을 시작한다. → Prompt 가 Resource 를 품는다.
  (B) Tool 호출 안내 — compare_departments
      지시문 텍스트만 반환. LLM 이 알맞은 Tool(department_stats)을 직접 호출.

클라이언트가 하는 일 — 타입 평탄화:
  MCP 의 EmbeddedResource 는 Anthropic API 에 그대로 보낼 수 있는 타입이 아니다.
  그래서 get_prompt 가 돌려준 content 를 Anthropic 의 text 블록으로 변환해야 한다.
  prompt_content_to_block() 이 이 변환을 담당한다.

03 → 05 차이:
  - /prompts          서버가 노출한 Prompt 목록 + 인자 조회 (prompts/list)
  - /prompt <name> …  Prompt 실행 → 서버 메시지를 messages 에 추가 → 루프 (prompts/get)
  - 대화 메모리(messages 누적)는 03 그대로. Prompt 결과도 그 위에 쌓인다.

실행:
  python agent/05_prompts.py
  질문> /prompts
  질문> /prompt compare_departments
  질문> /prompt analyze_student_risk 20210001     # 학번은 알고 있어야 함 (시드의 실제 학번)
  질문> /prompt course_catalog GSC
"""
import asyncio
import os
import platform
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"


def venv_python() -> Path:
    """MCP 서버 실행에 사용할 프로젝트 가상환경의 Python 경로."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def log(direction: str, text: str) -> None:
    """통신 흐름을 보기 쉽게 출력한다. >> 보냄, << 받음, ** 상태."""
    print(f"{direction} {text}", flush=True)


def mcp_tool_to_anthropic(tool) -> dict:
    """MCP Tool 정의를 Anthropic API 가 요구하는 tool 형식으로 바꾼다.

    입력 — tool: mcp.types.Tool
        .name        : str
        .description : str | None
        .inputSchema : dict        # JSON Schema

    반환 — dict (Anthropic messages.create 의 `tools` 목록에 들어갈 원소)
        {"name": str, "description": str, "input_schema": dict}
    """
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def extract_text_from_mcp_result(content_blocks) -> str:
    """MCP tools/call 응답의 content 블록들을 하나의 문자열로 합친다.

    입력 — content_blocks: list[ContentBlock]   (CallToolResult.content)
        TextContent       : .type="text",     .text: str
        ImageContent      : .type="image",    .data, .mimeType  (여기서는 repr 처리)
        EmbeddedResource  : .type="resource", .resource         (여기서는 repr 처리)
        AudioContent / ResourceLink: 같은 방식으로 repr 처리

    반환 — TextContent.text 는 줄바꿈으로 연결하고, 나머지 타입은 repr 로 표현한 문자열.
    """
    parts = []
    for b in content_blocks:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
        else:
            parts.append(repr(b))
    return "\n".join(parts)


def prompt_content_to_block(content) -> dict:
    """MCP PromptMessage.content 를 Anthropic content 블록으로 변환한다.

    MCP 와 Anthropic 은 서로 다른 프로토콜이므로 content 블록 타입도 다르다.
    특히 MCP 의 EmbeddedResource(URI + 본문)는 Anthropic 이 모르는 타입이다.
    그래서 text 로 변환하되, 어떤 자료였는지 알 수 있도록 URI 마커를 남긴다.

    변환 규칙:
        TextContent       → text (그대로)
        EmbeddedResource  → text ("[첨부 자료 URI]\\n본문")
        그 외             → repr (Image/Audio/ResourceLink 등은 여기서 단순 처리)

    NOTE: 호출자가 결과를 [block] 형태로 감싼다. Anthropic 의 message.content 는
    리스트이고, MCP PromptMessage.content 는 단일 블록이기 때문이다.
    """
    ctype = getattr(content, "type", None)
    if ctype == "text":
        return {"type": "text", "text": content.text}
    if ctype == "resource":  # EmbeddedResource
        res = content.resource
        text = getattr(res, "text", None)
        if text is not None:
            return {"type": "text", "text": f"[첨부 자료 {res.uri}]\n{text}"}
        return {"type": "text", "text": repr(res)}
    return {"type": "text", "text": repr(content)}


async def run_turns(session, anthropic, tools_for_claude, messages) -> str:
    """질문 하나를 처리하는 multi-turn 루프. 03 과 같은 흐름이다."""
    for turn in range(1, MAX_TURNS + 1):
        response = await anthropic.messages.create(
            model=MODEL,
            max_tokens=2048,
            tools=tools_for_claude,
            messages=messages,
        )
        log("<<", f"turn {turn} — stop_reason={response.stop_reason}")

        if response.stop_reason != "tool_use":
            answer = "".join(b.text for b in response.content if b.type == "text")
            messages.append({"role": "assistant", "content": response.content})
            return answer

        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        tool_results = []
        for tu in tool_uses:
            log(">>", f"  MCP tools/call: {tu.name}({tu.input})")
            tc = await session.call_tool(tu.name, tu.input)
            result_text = extract_text_from_mcp_result(tc.content)
            log("<<", f"  MCP 결과 ({len(result_text)} 자)")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": [{"type": "text", "text": result_text}],
            })

        messages.append({"role": "user", "content": tool_results})

    return f"[!] {MAX_TURNS} 라운드 한도 도달."


async def cmd_prompts(session) -> None:
    """서버가 제공하는 Prompt 목록을 조회한다.

    Prompt 는 서버가 정의한 재사용 작업 흐름이며, 이름·설명·인자 명세를 가진다.
    이 함수는 목록만 보여주고, 실제 실행은 cmd_prompt() 에서 처리한다.

    반환 — await session.list_prompts() → ListPromptsResult
        .prompts: list[Prompt]
            .name        : str
            .description : str | None
            .arguments   : list[PromptArgument] | None
                .name        : str
                .description : str | None
                .required    : bool | None
    """
    log(">>", "prompts/list")
    res = await session.list_prompts()
    print("\n[프롬프트]  (/prompt <name> <인자...> 로 실행)")
    for p in res.prompts:
        args = " ".join(f"<{a.name}>" for a in (p.arguments or []))
        head = f"{p.name} {args}".strip()
        desc = (p.description or "").splitlines()[0]
        print(f"  {head}\n      {desc}")


async def cmd_prompt(session, prompt_args, messages, name, values) -> bool:
    """Prompt 를 실행하고 서버가 돌려준 메시지를 대화 이력에 추가한다.

    cmd_prompts() 가 목록 조회라면, 이 함수는 실제 실행 단계다. 서버가 미리 만든
    메시지 묶음(자료 + 지시문)을 받아 Anthropic 형식으로 바꾼 뒤 messages 에
    덧붙인다. 이후 run_turns() 가 그 컨텍스트를 바탕으로 답변을 만든다.
    성공이면 True, 인자 검증 / RPC 실패면 False.

    호출 — await session.get_prompt(name, arguments=arguments)
        name      : str                # 프롬프트 이름 (예: "analyze_student_risk")
        arguments : dict[str, str]     # 인자 dict (예: {"student_no": "20210001"})

    반환 — → GetPromptResult
        .description : str | None
        .messages    : list[PromptMessage]
            .role    : "user" | "assistant"
            .content : ContentBlock    # 단일 블록 (Anthropic 의 list 와 다름)
                TextContent       : type="text",     text: str
                EmbeddedResource  : type="resource", resource: TextResourceContents | BlobResourceContents
                (ImageContent / AudioContent / ResourceLink — 우리 케이스 아님)
    """
    argnames = prompt_args.get(name)
    if argnames is None:
        print(f"없는 프롬프트: {name}  (/prompts 로 목록 확인)")
        return False
    if len(values) != len(argnames):
        need = " ".join(f"<{a}>" for a in argnames) or "(인자 없음)"
        print(f"인자 개수 불일치. 사용법: /prompt {name} {need}")
        return False

    arguments = dict(zip(argnames, values))
    log(">>", f"prompts/get: {name}({arguments})")
    try:
        result = await session.get_prompt(name, arguments=arguments)
    except Exception as e:
        log("**", f"프롬프트 실행 실패: {e}")
        return False

    embedded = 0
    for m in result.messages:
        block = prompt_content_to_block(m.content)
        if getattr(m.content, "type", None) == "resource":
            embedded += 1
        messages.append({"role": m.role, "content": [block]})
    log("<<", f"prompts/get 결과: {len(result.messages)} 메시지 seed "
              f"(임베디드 자료 {embedded}개 평탄화)")
    return True


async def repl() -> None:
    server_params = StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )

    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            # Prompt 목록을 미리 받아 인자 이름을 저장해 둔다 (/prompt 파싱에 사용).
            prompts_result = await session.list_prompts()
            prompt_args = {
                p.name: [a.name for a in (p.arguments or [])]
                for p in prompts_result.prompts
            }
            log("**", f"프롬프트 목록: {list(prompt_args)}")

            client = AsyncAnthropic()

            # 대화 이력은 03 과 동일하게 유지한다. Prompt 로 추가한 메시지도 여기에 쌓인다.
            messages: list[dict] = []

            print("\n=== 대화 시작 ===")
            print("  /prompts            서버가 노출한 Prompt 목록")
            print("  /prompt <name> …    Prompt 실행 (예: /prompt analyze_student_risk 20210001)")
            print("  /reset              대화 초기화")
            print("  /quit               종료")
            while True:
                try:
                    line = input("\n질문> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n종료합니다.")
                    break

                if not line:
                    continue

                # ── 명령 처리 ──────────────────────────────────────────
                if line == "/quit":
                    print("종료합니다.")
                    break
                if line == "/reset":
                    messages = []
                    print("대화를 초기화했습니다.")
                    continue
                if line == "/prompts":
                    await cmd_prompts(session)
                    continue
                if line.startswith("/prompt"):
                    parts = line.split()
                    if len(parts) < 2:
                        print("사용법: /prompt <name> <인자...>   (/prompts 로 목록)")
                        continue
                    name, values = parts[1], parts[2:]
                    seeded = await cmd_prompt(
                        session, prompt_args, messages, name, values
                    )
                    if not seeded:
                        continue
                    # Prompt 메시지가 대화에 추가되었으므로 바로 루프를 돌려 답변을 만든다.
                    log("**", f"(대화 {len(messages)} 메시지)")
                    answer = await run_turns(session, client, tools_for_claude, messages)
                    print(f"\n답변> {answer}")
                    continue

                # ── 자유 질문 처리 (03 과 동일) ─────────────────────────
                messages.append({"role": "user", "content": line})
                log("**", f"(대화 {len(messages)} 메시지)")
                answer = await run_turns(session, client, tools_for_claude, messages)
                print(f"\n답변> {answer}")


def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[X] ANTHROPIC_API_KEY 가 .env 에 없습니다.", file=sys.stderr)
        print("    https://console.anthropic.com/settings/keys 에서 발급 후 .env 에 추가.", file=sys.stderr)
        return 1
    asyncio.run(repl())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
