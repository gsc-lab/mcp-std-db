"""
Stage 3 — Prompt 실행 REPL (user-controlled 워크플로)

MCP primitive 트리오의 마지막 — Prompt. 누가 호출을 결정하느냐로 셋을 갈라보면:
  - Tool     (01~03) = 모델이 turn 중에 호출        (model-controlled)
  - Resource (04)     = 사용자가 /attach 로 컨텍스트 주입 (app/user-controlled)
  - Prompt   (이 파일)= 사용자가 *고르는* 재사용 템플릿   (user-controlled)

Prompt 는 Claude Desktop/Code 에서 **슬래시 명령**으로 뜨는 바로 그것이다. 사용자가
이름 + 인자로 트리거하면, 서버가 *미리 만들어둔 메시지 목록* 을 돌려준다. 우리는 그걸
대화(messages)에 심고(seed) 평소 multi-turn 루프를 돌린다. 그래서 여기선 prompt 가
literally 슬래시 명령 — /prompt <name> <인자...> 로 실행한다.

서버의 두 가지 Prompt 패턴 (server/main.py 참고):
  (A) EmbeddedResource 포함 — analyze_student_risk, course_catalog
      서버가 Resource(students://, courses://)를 *미리 읽어* 메시지에 박아 보낸다.
      LLM 은 추가 호출 없이 데이터를 받아 분석을 시작. → Prompt 가 Resource 를 품는다.
  (B) Tool 호출 안내 — compare_departments
      지시문 텍스트만 반환. LLM 이 알맞은 Tool(department_stats)을 직접 호출.

클라이언트가 하는 일 — 타입 평탄화:
  MCP 의 EmbeddedResource 는 Anthropic API 의 타입이 아니다. 그래서 get_prompt 가
  돌려준 메시지의 content 를 Anthropic 의 text 블록으로 *변환(flatten)* 해야 한다.
  prompt_content_to_block() 이 그 다리다.

03 → 05 차이:
  - /prompts          서버가 노출한 Prompt 목록 + 인자 조회 (prompts/list)
  - /prompt <name> …  Prompt 실행 → 서버 메시지를 messages 에 seed → 루프 (prompts/get)
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
    """프로젝트 venv 의 파이썬 경로."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def log(direction: str, text: str) -> None:
    """통신 로그.  >> 보냄,  << 받음,  ** 상태 변화."""
    print(f"{direction} {text}", flush=True)


def mcp_tool_to_anthropic(tool) -> dict:
    """MCP Tool 정의 → Anthropic API 의 tool 형식 (필드 이름만 변환)."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def extract_text_from_mcp_result(content_blocks) -> str:
    """MCP tools/call 응답에서 텍스트만 추출."""
    parts = []
    for b in content_blocks:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
        else:
            parts.append(repr(b))
    return "\n".join(parts)


def prompt_content_to_block(content) -> dict:
    """MCP PromptMessage 의 content 한 개 → Anthropic content 블록(text) 으로 평탄화.

    Prompt 메시지의 content 는 TextContent | ImageContent | EmbeddedResource 중 하나.
    Anthropic API 는 MCP 의 EmbeddedResource 타입을 모르므로, 임베디드 자료는
    그 안의 텍스트를 꺼내 일반 text 블록으로 펼친다. (이게 Prompt↔Resource 의 다리)
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
    """한 질문에 대한 multi-turn 루프 (03 과 동일). messages 를 누적하고 답변 반환."""
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


async def cmd_prompts(session, prompt_args: dict[str, list[str]]) -> None:
    """서버가 노출한 Prompt 카탈로그 출력 — Desktop 의 슬래시 명령 메뉴에 해당."""
    log(">>", "prompts/list")
    res = await session.list_prompts()
    print("\n[프롬프트]  (/prompt <name> <인자...> 로 실행)")
    for p in res.prompts:
        args = " ".join(f"<{a}>" for a in prompt_args.get(p.name, []))
        head = f"{p.name} {args}".strip()
        desc = (p.description or "").splitlines()[0]
        print(f"  {head}\n      {desc}")


async def cmd_prompt(session, prompt_args, messages, name, values) -> bool:
    """Prompt 실행 → 서버 메시지를 messages 에 seed. 실행했으면 True.

    get_prompt 가 돌려준 메시지(서버가 미리 만든 것)를 Anthropic 형식으로 평탄화해
    대화 이력에 덧붙인다. 이후 호출자가 run_turns 로 루프를 돌리면 된다.
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

            # Prompt 카탈로그를 미리 받아 인자 이름을 캐싱 (/prompt 파싱에 사용).
            prompts_result = await session.list_prompts()
            prompt_args = {
                p.name: [a.name for a in (p.arguments or [])]
                for p in prompts_result.prompts
            }
            log("**", f"프롬프트 목록: {list(prompt_args)}")

            client = AsyncAnthropic()

            # 대화 메모리 (03 과 동일) — Prompt 로 seed 한 메시지도 여기 누적된다.
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
                    await cmd_prompts(session, prompt_args)
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
                    # Prompt 가 대화를 seed 했으니 그대로 루프를 돌려 답변 생성.
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
