"""
Stage 3 — 대화형 REPL (대화 기억)

02_multi_turn 은 실행할 때마다 질문 하나에 답하고 종료한다.
03_repl 은 대화 이력을 계속 유지한다. messages 리스트를 입력 루프 밖에 두기 때문에
이전 질문과 답변이 다음 turn 에도 남는다.
그래서 "방금 그 학과 중 최저는?" 같은 후속 질문도 이전 맥락을 바탕으로 답할 수 있다.

핵심 — CLI 에서는 대화 기억이 단순하다:
  하나의 프로세스가 계속 살아 있으므로 messages 리스트만 유지하면 된다.
  Redis 같은 외부 저장소는 필요하지 않다. 그런 저장소는 요청마다 상태가 끊기는
  stateless 웹 서버에서 주로 필요하다.

02 → 03 차이:
  - run_agent(question) 1회 → while 루프 안에서 반복
  - messages 가 루프 밖에 선언되어 turn 간 누적된다 (이것이 대화 기억의 핵심).
  - /reset 으로 대화 초기화, /quit 으로 종료

전체 절차:
  1) MCP 서버 1회 연결 (REPL 동안 유지)
  2) while: 질문 입력 → multi-turn 루프 → 답변 출력 → messages 누적 → 반복

실행:
  python agent/03_repl.py
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


async def run_turns(session, anthropic, tools_for_claude, messages) -> str:
    """질문 하나를 처리하는 multi-turn 루프. 02 의 흐름과 같다.

    messages 는 호출자가 만든 리스트다. 이 함수는 assistant 응답과 tool_result 를
    그 리스트에 덧붙이기만 한다. 그래서 함수가 끝난 뒤에도 대화 이력이 남아
    다음 질문으로 이어진다.

    내부 MCP 호출:
        await session.call_tool(name, arguments) → CallToolResult
            .content : list[ContentBlock]    # 텍스트는 .text
            .isError : bool                  # 실패 여부 (여기서는 검사 생략)
    """
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


async def repl() -> None:
    server_params = StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )

    # MCP 서버는 REPL 이 실행되는 동안 한 번만 연결해 계속 사용한다.
    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # await session.initialize() → InitializeResult (서버 정보 + capabilities)
            await session.initialize()
            log("**", "MCP initialize 완료")

            # await session.list_tools() → ListToolsResult
            #   .tools : list[Tool]   (name, description, inputSchema)
            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            client = AsyncAnthropic()

            # 대화 기억의 핵심: 이 리스트가 while 루프 밖에 있다.
            # 매 질문과 답변이 여기에 누적되므로 다음 질문이 이전 맥락을 볼 수 있다.
            # CLI 프로세스가 살아 있는 동안은 별도 DB 없이 이 리스트만으로 충분하다.
            messages: list[dict] = []

            print("\n=== 대화 시작 ===  (/reset 초기화, /quit 종료)")
            while True:
                try:
                    question = input("\n질문> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n종료합니다.")
                    break

                if question == "/quit":
                    print("종료합니다.")
                    break
                if question == "/reset":
                    messages = []
                    print("대화를 초기화했습니다.")
                    continue
                if not question:
                    continue

                # 질문을 대화 이력에 추가한 뒤 multi-turn 루프를 실행한다.
                messages.append({"role": "user", "content": question})
                log("**", f"(현재 대화 이력 {len(messages)} 메시지)")
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
