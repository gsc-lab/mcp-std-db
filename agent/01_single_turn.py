"""
Stage 3 — 도구를 한 번만 호출하는 에이전트 (single-turn)

MCP 서버를 자식 프로세스로 실행하고 Anthropic API 와 통신하여 사용자 질문에 답한다.
이 예제에서는 도구 호출을 최대 1번만 허용한다.

전체 절차:
  1) MCP 서버 시작 (stdio 로 연결)
  2) initialize 핸드셰이크 + tools/list 로 도구 목록 받기
  3) Anthropic API 에 질문과 도구 명세 전송
  4) 응답에 tool_use 가 있으면 MCP 서버에 tools/call 요청
  5) 도구 결과를 다시 Anthropic API 에 전송 (tools 인자 제외 = 추가 호출 차단)
  6) 최종 답변 출력

도구를 여러 번 호출해야 하는 경우는 02_multi_turn.py 를 참고한다.
JSON-RPC 메시지를 직접 보고 싶다면 00_raw_jsonrpc.py 를 참고한다.

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

# Windows 콘솔에서 한글이 깨지지 않도록 UTF-8 출력으로 맞춘다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
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
    # MCP 서버 실행 파라미터. PYTHONUTF8=1 로 한글 소스와 입출력을 UTF-8 로 맞춘다.
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
            # 2) 핸드셰이크를 마친 뒤, 서버가 제공하는 도구 목록을 조회한다.
            # await session.initialize() → InitializeResult (서버 정보 + capabilities)
            await session.initialize()
            log("**", "MCP initialize 완료")

            # await session.list_tools() → ListToolsResult
            #   .tools : list[Tool]   (name, description, inputSchema)
            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            # 3) Anthropic API 에 질문과 도구 명세를 함께 보낸다.
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

            # 4) 모델이 도구를 요청하지 않았다면 바로 답변하고 종료한다.
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                answer = "".join(b.text for b in response.content if b.type == "text")
                print("\n=== 최종 답변 ===")
                print(answer)
                return

            # 모델 응답(tool_use 포함)을 대화 이력에 추가한다.
            messages.append({"role": "assistant", "content": response.content})

            # MCP 서버에 실제 도구 실행을 요청하고 결과를 모은다.
            # await session.call_tool(name, arguments) → CallToolResult
            #   .content : list[ContentBlock]   # 텍스트는 .text
            #   .isError : bool                 # 실패 여부 (학습용은 검사 생략)
            tool_results = []
            for tu in tool_uses:
                log(">>", f"MCP tools/call: {tu.name}({tu.input})")
                mcp_result = await session.call_tool(tu.name, tu.input)
                # NOTE: 실제 구현에서는 mcp_result.isError 도 확인해야 한다.
                result_text = extract_text_from_mcp_result(mcp_result.content)
                log("<<", f"MCP 결과 ({len(result_text)} 자)")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": [{"type": "text", "text": result_text}],
                })

            messages.append({"role": "user", "content": tool_results})

            # 5) 도구 결과를 다시 API 에 보낸다. 이때 tools 인자를 빼서 추가 호출을 막는다.
            #    이것이 single-turn 예제의 핵심이다. 여러 번 허용하는 버전은 02_multi_turn.py.
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
