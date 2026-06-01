"""
Stage 3 — 도구를 여러 번 호출할 수 있는 에이전트 (multi-turn)

LLM 이 Tool 을 여러 번 호출해야 하는 상황을 처리한다. 01 의 단일 분기(if)를
for 루프로 바꾸고, LLM 이 stop_reason="end_turn" 으로 답변을 마칠 때까지
도구 호출을 반복한다.

01 → 02 차이:
  - 매 API 호출마다 tools 인자를 계속 전달한다 (01 은 두 번째 호출에서 제외).
  - 종료 여부는 LLM 의 stop_reason 으로 판단한다.
  - MAX_TURNS 로 최대 반복 횟수를 제한해 무한 반복을 막는다.

전체 절차:
  1) MCP 서버 시작 + 도구 목록 조회
  2) Anthropic API 호출 (질문 + 도구 명세)
  3) stop_reason 검사
     - "tool_use" 가 아니면 답변을 출력하고 종료
     - "tool_use" 이면 도구 실행 → 결과를 대화 이력에 추가 → 다시 (2) 로

예시 질문 — 여러 도구를 이어서 호출:
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
MAX_TURNS = 10  # 안전장치. 보통은 2~4회 안에 종료된다.
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


async def run_agent(question: str) -> None:
    server_params = StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )

    # 1) MCP 서버를 시작하고 서버가 제공하는 도구 목록을 가져온다.
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
            messages = [{"role": "user", "content": question}]
            log(">>", f"질문: {question!r}")

            # 2~3) 다중 호출 루프
            # stop_reason 이 "tool_use" 가 아니면 모델이 더 이상 도구를 원하지 않는다는 뜻이다.
            # 매 호출에 tools 인자를 유지하므로 모델은 필요할 때 계속 도구를 요청할 수 있다.
            for turn in range(1, MAX_TURNS + 1):
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=2048,
                    tools=tools_for_claude,
                    messages=messages,
                )
                log("<<", f"turn {turn} — stop_reason={response.stop_reason}")

                # 종료 조건: 도구 호출이 아니라 최종 답변이 나온 경우.
                if response.stop_reason != "tool_use":
                    answer = "".join(b.text for b in response.content if b.type == "text")
                    print(f"\n=== 최종 답변 (총 {turn} 라운드) ===")
                    print(answer)
                    return

                # 모델 응답(tool_use 포함)을 대화 이력에 추가한다.
                messages.append({"role": "assistant", "content": response.content})

                # 이번 라운드에서 요청된 도구를 실행한다. 병렬 도구 호출이면 여러 개가 들어온다.
                # await session.call_tool(name, arguments) → CallToolResult
                #   .content : list[ContentBlock]   # 텍스트는 .text
                #   .isError : bool                 # 실패 여부 (학습용은 검사 생략)
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                tool_results = []
                for tu in tool_uses:
                    log(">>", f"  MCP tools/call: {tu.name}({tu.input})")
                    tc = await session.call_tool(tu.name, tu.input)
                    # NOTE: 실제 구현에서는 tc.isError 도 확인해야 한다.
                    result_text = extract_text_from_mcp_result(tc.content)
                    log("<<", f"  MCP 결과 ({len(result_text)} 자)")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": [{"type": "text", "text": result_text}],
                    })

                messages.append({"role": "user", "content": tool_results})
                log("**", f"  다음 라운드 (대화 이력 {len(messages)} 메시지)")

            # MAX_TURNS 를 넘으면 도구 호출이 끝나지 않은 것으로 보고 중단한다.
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
