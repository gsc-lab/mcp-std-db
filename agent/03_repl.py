"""
Stage 3 — 대화형 REPL (대화 메모리)

02_multi_turn 은 매 실행이 독립이다 — 질문 하나 답하고 종료.
03_repl 은 *대화를 기억* 한다. messages 리스트를 입력 루프 *밖* 에 두어 turn 간 유지.
그래서 "방금 그 학과 중 최저는?" 같은 후속 질문이 이전 맥락을 인지한다.

핵심 — CLI 에선 대화 메모리가 "공짜":
  단일 프로세스가 계속 살아있으므로 messages 리스트만 들고 있으면 끝이다.
  Redis 같은 외부 저장소는 *불필요*. 그건 매 요청이 독립인 *stateless 웹 서버* 의
  문제이지, 한 프로세스가 도는 CLI 의 문제가 아니다.

02 → 03 차이:
  - run_agent(question) 1회 → while 루프 안에서 반복
  - messages 가 루프 *밖* 에 선언되어 turn 간 누적 (이게 대화 메모리의 전부)
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


async def run_turns(session, anthropic, tools_for_claude, messages) -> str:
    """한 질문에 대한 multi-turn 루프 (02 와 동일). messages 를 누적하고 답변 반환.

    messages 는 *호출자가 소유* 한다. 이 함수는 그 리스트에 assistant/tool_result 를
    덧붙이기만 한다 → 호출 후에도 대화 이력이 남아 다음 질문에 이어진다.
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

    # MCP 서버는 REPL 동안 한 번만 연결해 유지.
    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            client = AsyncAnthropic()

            # ★ 대화 메모리의 전부 — 이 리스트가 while 루프 *밖* 에 있다.
            #   매 질문이 여기에 누적되어 다음 질문이 이전 맥락을 본다.
            #   Redis 도, DB 도 필요 없음. 프로세스가 살아있는 동안 그냥 유지됨.
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

                # 질문을 대화 이력에 추가 → multi-turn 실행 → 답변
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
