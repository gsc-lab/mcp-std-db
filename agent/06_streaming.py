"""
Stage 3 — 스트리밍으로 출력하는 REPL (SDK 의 사용자 경험 기능)

01~05 에서 MCP 의 세 가지 primitive(Tool/Resource/Prompt)는 모두 다뤘다.
이 파일은 새로운 MCP 개념을 추가하지 않는다. 05 와 MCP 흐름은 같고,
달라지는 것은 출력 방식 하나다. 응답 전체를 기다렸다가 출력하는 대신,
토큰이 도착하는 대로 화면에 보여준다.

주의 — 이것은 MCP 기능이 아니라 Anthropic SDK 의 사용자 경험 기능이다.
  prompts/get, tools/call, resources/read 같은 MCP 통신 흐름과는 별개다.
  스트리밍은 "모델 텍스트를 화면에 어떻게 보여줄 것인가" 의 문제다.

05 → 06 차이 (run_turns 한 곳뿐):
  - messages.create()  (응답 전체를 기다렸다가 반환)
      → messages.stream()  (async with 컨텍스트로 스트림 처리)
  - stream.text_stream     으로 텍스트 토큰을 실시간 print
  - stream.get_final_message() 로 tool_use 까지 포함된 최종 메시지를 복원
        → 복원된 객체는 create() 가 주던 response 와 동일 → 이후 루프 로직 그대로.
  - 답변이 run_turns 안에서 실시간 출력되므로 호출부는 다시 출력하지 않는다.
  - MAX_TOKENS 상향(2048→4096): 스트리밍 답변이 중간에 잘리면 어색하기 때문이다.

왜 tool_use 루프가 그대로 사는가:
  text_stream 은 텍스트만 흘려보낸다. tool_use 블록은 text_stream 에 직접 나오지 않고,
  get_final_message() 가 마지막에 완성 메시지로 재조립해 준다. 그래서 02~05 의
  "stop_reason 검사 → 도구 실행 → 결과 누적" 구조를 그대로 쓸 수 있다.

실행:
  python agent/06_streaming.py
  질문> /prompt compare_departments        # 긴 분석이 실시간으로 흘러나온다
  질문> GSC 학과 평균 GPA 와 상위 3명
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
MAX_TOKENS = 4096  # 05 는 2048. 스트리밍 답변은 중간에 잘리면 어색해 조금 늘린다.
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

    EmbeddedResource 는 Anthropic 이 모르는 타입이므로 text 로 변환하고 URI 마커를 남긴다.
    자세한 변환 규칙은 `05_prompts.py` 의 같은 이름 함수를 참고한다.
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


async def run_turns(session, anthropic, tools_for_claude, messages) -> None:
    """질문 하나를 처리하는 multi-turn 루프의 스트리밍 출력 버전.

    MCP 흐름은 05 와 같다. 차이는 Anthropic SDK 호출 방식뿐이다.
    messages.create() 는 완성된 응답을 반환하고, messages.stream() 은 토큰이 도착하는
    즉시 받을 수 있게 해 준다. 이 차이는 MCP 통신과는 무관하다.

    Anthropic SDK 스트리밍 API:
        async with anthropic.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:           # 텍스트 조각(chunk: str)
                print(text, end="", flush=True)
            response = await stream.get_final_message()     # Message — create() 와 동일 shape
                                                            # tool_use 같은 비-텍스트 블록도 여기서 복원
        stream                                              # MessageStreamManager (context manager)
        stream.text_stream                                  # AsyncIterator[str]  — *텍스트만* 흘림
        stream.get_final_message() → Message
            .content      : list[ContentBlock]   # text / tool_use 등 모두 포함
            .stop_reason  : "end_turn" | "tool_use" | ...
            .usage        : Usage                # 토큰 사용량

    답변은 이 함수 안에서 실시간으로 출력한다. 따라서 호출자는 결과를 다시 출력하지 않는다.
    tool_use 루프 구조는 02~05 와 동일하게 동작한다. 비-텍스트 블록은 text_stream 에
    나오지 않고, get_final_message() 가 마지막에 완성 메시지로 복원하기 때문이다.

    트레이드오프:
        + 첫 토큰이 도착하는 즉시 화면에 보여 줄 수 있다.
        + 긴 답변의 진행 가시화
        - 전체 응답 시간 자체는 거의 같다. 끝까지 받아야 완료된다.
        - async context manager 와 최종 메시지 복원 때문에 코드가 조금 길어진다.
    """
    for turn in range(1, MAX_TURNS + 1):
        # ── 스트리밍 (05 와의 유일한 차이) ─────────────────────────────
        #   1) stream() 컨텍스트에 진입해 응답이 오는 동안 스트림을 유지한다.
        #   2) text_stream 으로 텍스트 토큰을 실시간으로 받는다.
        #   3) get_final_message() 로 tool_use 등을 포함한 최종 Message 를 복원한다.
        #   복원된 response 는 create() 의 반환값과 같은 구조라 이후 로직은 그대로 쓴다.
        async with anthropic.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=tools_for_claude,
            messages=messages,
        ) as stream:
            streamed = False
            async for text in stream.text_stream:           # 토큰 단위로 도착한다.
                if not streamed:
                    print("\n답변> ", end="", flush=True)
                    streamed = True
                print(text, end="", flush=True)
            response = await stream.get_final_message()     # 완성된 Message 로 복원한다.
        if streamed:
            print()  # 실시간 출력한 줄을 닫는다 (개행)
        # ────────────────────────────────────────────────────────────
        log("<<", f"turn {turn} — stop_reason={response.stop_reason}")

        if response.stop_reason != "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            return

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

    print(f"\n[!] {MAX_TURNS} 라운드 한도 도달.")


async def cmd_prompts(session, prompt_args: dict[str, list[str]]) -> None:
    """서버가 제공하는 Prompt 목록을 출력한다.

    반환 구조 설명은 `05_prompts.py` 의 같은 이름 함수를 참고한다.
    06 의 핵심은 스트리밍이므로 Prompt 관련 코드는 거의 그대로 사용한다.
    """
    log(">>", "prompts/list")
    res = await session.list_prompts()
    print("\n[프롬프트]  (/prompt <name> <인자...> 로 실행)")
    for p in res.prompts:
        args = " ".join(f"<{a}>" for a in prompt_args.get(p.name, []))
        head = f"{p.name} {args}".strip()
        desc = (p.description or "").splitlines()[0]
        print(f"  {head}\n      {desc}")


async def cmd_prompt(session, prompt_args, messages, name, values) -> bool:
    """Prompt 를 실행하고 서버 메시지를 messages 에 추가한다.

    호출 인자와 반환 구조 설명은 `05_prompts.py` 의 같은 이름 함수를 참고한다.
    06 의 핵심은 스트리밍이므로 이 함수의 역할은 05 와 같다.
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
                    # Prompt 메시지가 대화에 추가되었으므로 바로 루프를 돌려 실시간 답변을 만든다.
                    log("**", f"(대화 {len(messages)} 메시지)")
                    await run_turns(session, client, tools_for_claude, messages)
                    continue

                # ── 자유 질문 처리 ──────────────────────────────────────
                messages.append({"role": "user", "content": line})
                log("**", f"(대화 {len(messages)} 메시지)")
                await run_turns(session, client, tools_for_claude, messages)


def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[X] ANTHROPIC_API_KEY 가 .env 에 없습니다.", file=sys.stderr)
        print("    https://console.anthropic.com/settings/keys 에서 발급 후 .env 에 추가.", file=sys.stderr)
        return 1
    asyncio.run(repl())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
