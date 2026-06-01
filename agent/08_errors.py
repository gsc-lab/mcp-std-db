"""
Stage 3 — 에러 처리 강화 (실서비스에 한 걸음 가까운 버전)

03_repl.py 의 정상 흐름 코드는 그대로 두고, 에러 분기만 추가한 버전이다.
앞 단계에서 NOTE 로 남겨 둔 "실제 구현에서는 isError 검사 필요" 같은 부분을
여기서 실제 코드로 다룬다.

다섯 가지 에러 케이스:

  (1) MCP 도구 실행 실패 — CallToolResult.isError == True
       서버 도구에서 예외, DB 오류, 검증 실패가 발생한 경우다.
       tool_result 에 is_error=True 를 달면 모델이 재시도할지 포기할지 판단할 수 있다.

  (2) MCP 도구 타임아웃 — asyncio.wait_for 로 한도
       서버가 멈췄거나 너무 느릴 때 클라이언트를 보호한다.
       타임아웃도 is_error 로 모델에 알려야 다른 시도를 할 수 있다.

  (3) stop_reason == "max_tokens" — 응답이 잘림
       답변이 도중에 끊긴 경우다. 잘린 답을 다시 모델 입력으로 넣으면 혼란스러울 수
       있으므로 루프를 더 돌리지 않는다. 잘린 텍스트와 경고를 함께 보여 준다.

  (4) stop_reason == "refusal" — 모델이 거부
       Claude 가 안전상 응답을 거부한 경우다. 추가 처리를 하지 않고 사용자에게 알린다.

  (5) MAX_TURNS 초과 — 도구 호출이 안 끝남
       모델이 도구만 계속 호출하고 최종 답을 내지 못하는 경우다. 무한 루프를 막는다.

추가로 Anthropic API 호출 자체의 예외(네트워크, rate limit, HTTP 오류)도
APIError 로 묶어 사용자에게 읽기 쉬운 메시지를 보여 준다.

체감 시연:
  질문> status 가 active 인 학생 찾아줘
    → 모델이 search_students(status="active") 호출
    → 서버: enum 에 active 없음 (실제 값: enrolled/leave/graduated/dropped)
       → psycopg 가 InvalidTextRepresentation 발생 → isError=True
    → 클라이언트가 tool_result.is_error=True 로 모델에 전달
    → 모델: "active 가 잘못된 값이군" 판단 → 올바른 값으로 재시도
    → 정상 결과 받고 사용자에게 답변

03 → 08 차이:
  - call_tool_safe() 헬퍼: isError + 타임아웃 + RPC 예외를 (text, is_error) 로 통일
  - run_turns 의 stop_reason 분기: 2가지(tool_use / 그 외) → 5가지로 확장
  - tool_result 블록에 is_error 키 추가
  - Anthropic API 예외(APIError) 캡처
  - MAX_TURNS 메시지 친절화

실행:
  python agent/08_errors.py
"""
import asyncio
import os
import platform
import sys
from pathlib import Path

from anthropic import APIError, AsyncAnthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10
TOOL_TIMEOUT_S = 30.0   # MCP 도구 한 번 호출을 기다리는 최대 시간(초)
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
    """MCP Tool 정의를 Anthropic API 의 tool 형식으로 바꾼다."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def extract_text_from_mcp_result(content_blocks) -> str:
    """MCP tools/call 응답의 content 블록들을 하나의 문자열로 합친다."""
    parts = []
    for b in content_blocks:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
        else:
            parts.append(repr(b))
    return "\n".join(parts)


async def call_tool_safe(session, name: str, args: dict) -> tuple[str, bool]:
    """MCP 도구 호출 결과를 (텍스트, is_error) 형태로 통일한다.

    세 가지 실패 경로를 모두 (text, True) 형태로 바꾼다. 호출자가 이것을
    tool_result.is_error=True 로 모델에 넘기면, 모델이 재시도할지 포기할지 판단한다.

        (1) 타임아웃        : asyncio.TimeoutError
        (2) RPC 자체 실패    : 그 외 예외 (서버 죽음, 연결 끊김 등)
        (3) 도구의 isError  : RPC 는 성공했지만 서버가 실패를 표시

    호출 — await asyncio.wait_for(session.call_tool(...), timeout=...)
        TimeoutError 가 발생하면 기다리던 작업은 취소된다.

    반환 — tuple[str, bool]
        text     : 모델에 보낼 텍스트 (에러면 사람이 읽을 만한 설명)
        is_error : True 면 tool_result.is_error 로 전달해 실패임을 표시
    """
    try:
        tc = await asyncio.wait_for(
            session.call_tool(name, args),
            timeout=TOOL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return (
            f"[타임아웃] 도구 {name} 가 {TOOL_TIMEOUT_S}s 안에 응답하지 않음. "
            "다른 도구를 시도하거나 인자를 단순화해 보세요.",
            True,
        )
    except Exception as e:
        # MCP 서버가 죽었거나 RPC 자체가 실패. 학습용은 클래스명까지만 노출.
        return (
            f"[RPC 에러] {type(e).__name__}: {e}",
            True,
        )

    text = extract_text_from_mcp_result(tc.content)
    return text, bool(tc.isError)


async def run_turns(session, anthropic, tools_for_claude, messages) -> str:
    """multi-turn 루프에 주요 에러 분기를 추가한 버전.

    stop_reason 별 처리:
        end_turn    : 정상 완료. assistant 텍스트 반환.
        tool_use    : 도구 실행 → 결과 누적 → 다음 turn.
        max_tokens  : 답이 잘림. 잘린 텍스트와 경고를 반환하고 루프를 중단.
                       잘린 답을 모델 입력으로 다시 넣지 않는다.
        refusal     : 모델이 거부. 사용자에게 알리고 종료.
        그 외        : pause_turn / stop_sequence 등. 드물지만 명시적으로 처리.

    Anthropic API 예외 (네트워크/rate limit/HTTP):
        APIError 하나로 묶어 사용자에게 읽기 쉬운 메시지로 반환한다.
        세부: APIConnectionError, RateLimitError, APIStatusError, ...
        재시도/백오프는 SDK 의 max_retries 옵션이나 별도 정책으로 다루면 된다.
    """
    for turn in range(1, MAX_TURNS + 1):
        # ── Anthropic API 호출: 네트워크/API 예외를 사용자 메시지로 바꾼다. ──
        try:
            response = await anthropic.messages.create(
                model=MODEL,
                max_tokens=2048,
                tools=tools_for_claude,
                messages=messages,
            )
        except APIError as e:
            return f"[X] Anthropic API 에러: {type(e).__name__}: {e}"

        log("<<", f"turn {turn} — stop_reason={response.stop_reason}")
        stop = response.stop_reason

        # ── 정상 완료: 최종 답변을 반환한다. ───────────────────────
        if stop == "end_turn":
            messages.append({"role": "assistant", "content": response.content})
            return "".join(b.text for b in response.content if b.type == "text")

        # ── 응답 잘림: 루프를 더 돌리지 않는다. ───────────────────
        if stop == "max_tokens":
            # 잘린 텍스트도 이력에 남겨 후속 질문이 맥락을 가질 수 있게 한다.
            messages.append({"role": "assistant", "content": response.content})
            answer = "".join(b.text for b in response.content if b.type == "text")
            return f"{answer}\n\n[!] max_tokens 도달 — 답변이 도중에 잘렸습니다."

        # ── 모델 거부: 그대로 사용자에게 알린다. ──────────────────
        if stop == "refusal":
            messages.append({"role": "assistant", "content": response.content})
            return "[X] 모델이 응답을 거부했습니다 (refusal)."

        # ── 예상하지 못한 stop_reason: 안전하게 처리를 중단한다. ──
        if stop != "tool_use":
            return f"[?] 예상치 못한 stop_reason={stop!r} — 응답 처리를 중단합니다."

        # ── 도구 실행: call_tool_safe 가 모든 실패를 is_error 로 정리한다. ──
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        tool_results = []
        for tu in tool_uses:
            log(">>", f"  MCP tools/call: {tu.name}({tu.input})")
            text, is_error = await call_tool_safe(session, tu.name, tu.input)
            marker = "  [is_error]" if is_error else ""
            log("<<", f"  MCP 결과 ({len(text)} 자){marker}")

            # is_error=True 면 모델이 "이 도구 호출은 실패했다" 고 인식할 수 있다.
            # 03 까지는 이 키가 없어 에러 텍스트를 정상 결과로 오해할 여지가 있었다.
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": [{"type": "text", "text": text}],
                "is_error": is_error,
            })

        messages.append({"role": "user", "content": tool_results})

    # ── MAX_TURNS 안전장치 ─────────────────────────────────────────
    return (
        f"[!] MAX_TURNS={MAX_TURNS} 도달 — 모델이 도구 호출을 마치지 못했습니다. "
        "질문을 더 구체적으로 좁히거나 /reset 으로 대화를 초기화해 보세요."
    )


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

            client = AsyncAnthropic()
            messages: list[dict] = []

            print("\n=== 대화 시작 (에러 처리 강화) ===  (/reset 초기화, /quit 종료)")
            print("  시연 질문 예:")
            print("    'status 가 active 인 학생 찾아줘'")
            print("       → 서버 enum 에 없는 값 → isError → 모델이 올바른 값으로 재시도")
            print("    '학과별 평균 GPA 비교해줘'")
            print("       → 정상 흐름 (에러 분기는 아무것도 안 탐)")
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
