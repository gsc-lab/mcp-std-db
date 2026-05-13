"""
Stage 3 — 다중 호출 에이전트 (multi-turn)

LLM 이 Tool 을 여러 번 호출하는 경우를 지원한다. 01 의 단일 분기(if) 를 for 루프로
바꿔, LLM 이 stop_reason="end_turn" 으로 답변을 마칠 때까지 도구 호출을 반복한다.

01 → 02 차이:
  - 매 API 호출에 tools 인자를 유지 (01 은 두 번째 호출에서 제외했음)
  - 종료 조건이 LLM 의 stop_reason 으로 결정됨
  - 안전망: MAX_TURNS 로 최대 반복 횟수 제한

전체 절차:
  1) MCP 서버 시작 + 도구 목록 조회
  2) Anthropic API 호출 (질문 + 도구 명세)
  3) stop_reason 검사
     - "tool_use" 가 아니면 답변 출력 후 종료
     - "tool_use" 면 도구 실행 → 결과를 대화 이력에 추가 → 다시 (2) 로

예시 질문 — 여러 도구를 연쇄 호출:
  "GSC 학과의 평균 GPA 와 상위 3명을 같이 보여줘"
  → turn 1: department_stats() 호출
  → turn 2: top_students(department_code="GSC", limit=3) 호출
  → turn 3: 결과 종합 후 답변 (stop_reason=end_turn)

실행:
  python agent/02_multi_turn.py "GSC 학과의 평균 GPA 와 상위 3명"
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
MAX_TURNS = 10  # 안전망 — 일반적으로 2~4 회 안에 종료.
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


async def run_agent(question: str) -> None:
    server_params = StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )

    # 1) MCP 서버 시작 + 도구 목록
    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            client = AsyncAnthropic()
            messages = [{"role": "user", "content": question}]
            log(">>", f"질문: {question!r}")

            # 2~3) 다중 호출 루프
            # 종료 조건: stop_reason 이 "tool_use" 가 아닐 때.
            # 매 호출에 tools 인자를 유지하므로 LLM 이 도구 호출을 계속할 수 있다.
            for turn in range(1, MAX_TURNS + 1):
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=2048,
                    tools=tools_for_claude,
                    messages=messages,
                )
                log("<<", f"turn {turn} — stop_reason={response.stop_reason}")

                # 종료 조건
                if response.stop_reason != "tool_use":
                    answer = "".join(b.text for b in response.content if b.type == "text")
                    print(f"\n=== 최종 답변 (총 {turn} 라운드) ===")
                    print(answer)
                    return

                # LLM 응답 (tool_use 포함) 을 이력에 추가
                messages.append({"role": "assistant", "content": response.content})

                # 이번 라운드의 도구 호출 처리 (parallel tool use 면 여러 개)
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                tool_results = []
                for tu in tool_uses:
                    log(">>", f"  MCP tools/call: {tu.name}({tu.input})")
                    tc = await session.call_tool(tu.name, tu.input)
                    # NOTE: 실제 구현에선 tc.isError 검사 필요.
                    result_text = extract_text_from_mcp_result(tc.content)
                    log("<<", f"  MCP 결과 ({len(result_text)} 자)")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": [{"type": "text", "text": result_text}],
                    })

                messages.append({"role": "user", "content": tool_results})
                log("**", f"  다음 라운드 (대화 이력 {len(messages)} 메시지)")

            # MAX_TURNS 초과
            print(
                f"\n[!] {MAX_TURNS} 라운드 한도 도달 — 모델이 도구 호출을 멈추지 못함.",
                file=sys.stderr,
            )


def main() -> int:
    if len(sys.argv) < 2:
        print('사용법: python agent/02_multi_turn.py "질문 내용"', file=sys.stderr)
        print('예시:  python agent/02_multi_turn.py "GSC 학과 평균 GPA 와 상위 3명"', file=sys.stderr)
        return 1
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[X] ANTHROPIC_API_KEY 가 .env 에 없습니다.", file=sys.stderr)
        print("    https://console.anthropic.com/settings/keys 에서 발급 후 .env 에 추가.", file=sys.stderr)
        return 1
    question = " ".join(sys.argv[1:])
    asyncio.run(run_agent(question))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
