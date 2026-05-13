"""
Stage 3 — 단일 호출 에이전트 (single-turn)

MCP 서버를 자식 프로세스로 띄우고, Anthropic API 와 통신하여 사용자 질문에 답한다.
도구 호출은 정확히 1번만 허용한다.

전체 절차:
  1) MCP 서버 시작 (stdio 로 연결)
  2) initialize 핸드셰이크 + tools/list 로 도구 목록 받기
  3) Anthropic API 호출 — 질문 + 도구 명세 전송
  4) 응답에 tool_use 가 있으면 MCP 서버에 tools/call 위임
  5) 결과를 다시 Anthropic API 에 전송 (tools 인자 제외 = 추가 호출 차단)
  6) 최종 답변 출력

여러 번 도구 호출이 필요한 경우는 02_multi_turn.py 참조.
JSON-RPC 메시지를 직접 보려면 00_raw_jsonrpc.py 참조.

실행:
  python agent/01_single_turn.py "GSC 학과의 학생 수와 평균 GPA"
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

# Windows 콘솔의 한글 출력 깨짐 방지.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"


def venv_python() -> Path:
    """프로젝트 venv 의 파이썬 경로. MCP 서버는 이 인터프리터로 실행된다."""
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
    # MCP 서버 실행 파라미터. PYTHONUTF8=1 은 한글 소스 디코딩을 강제.
    server_params = StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )

    # 1) MCP 서버 시작
    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # 2) 핸드셰이크 + 도구 목록 조회
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            # 3) Anthropic API 호출 — 질문 + 도구 명세
            client = AsyncAnthropic()
            messages = [{"role": "user", "content": question}]
            log(">>", f"질문: {question!r}")

            response = await client.messages.create(
                model=MODEL,
                max_tokens=2048,
                tools=tools_for_claude,
                messages=messages,
            )
            log("<<", f"응답 stop_reason={response.stop_reason}")

            # 4) tool_use 가 없으면 그대로 답변하고 종료
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                answer = "".join(b.text for b in response.content if b.type == "text")
                print("\n=== 최종 답변 ===")
                print(answer)
                return

            # LLM 응답(tool_use 포함) 을 대화 이력에 추가
            messages.append({"role": "assistant", "content": response.content})

            # MCP 서버에 도구 호출을 위임하고 결과를 모은다
            tool_results = []
            for tu in tool_uses:
                log(">>", f"MCP tools/call: {tu.name}({tu.input})")
                mcp_result = await session.call_tool(tu.name, tu.input)
                # NOTE: 실제 구현에선 mcp_result.isError 검사 필요. 학습용은 단순화.
                result_text = extract_text_from_mcp_result(mcp_result.content)
                log("<<", f"MCP 결과 ({len(result_text)} 자)")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": [{"type": "text", "text": result_text}],
                })

            messages.append({"role": "user", "content": tool_results})

            # 5) 결과를 다시 API 에 전송 — tools 인자 제외하여 추가 호출을 차단
            #    이것이 single-turn 의 정의. 여러 번 허용은 02_multi_turn.py.
            log(">>", "Anthropic API 재호출 (tools 제외 = 최종 답변 강제)")
            response2 = await client.messages.create(
                model=MODEL,
                max_tokens=2048,
                messages=messages,
            )
            log("<<", f"최종 stop_reason={response2.stop_reason}")

            # 6) 최종 답변 출력
            answer = "".join(b.text for b in response2.content if b.type == "text")
            print("\n=== 최종 답변 ===")
            print(answer)


def main() -> int:
    if len(sys.argv) < 2:
        print('사용법: python agent/01_single_turn.py "질문 내용"', file=sys.stderr)
        print('예시:  python agent/01_single_turn.py "GSC 학과의 학생 수와 평균 GPA"', file=sys.stderr)
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
