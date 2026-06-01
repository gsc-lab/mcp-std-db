"""
Stage 3 — 도구를 동시에 실행하기 (asyncio.gather)

지금까지 02~08 의 도구 실행 블록은 모두 순차 실행이었다:

    for tu in tool_uses:
        tc = await session.call_tool(tu.name, tu.input)   # 한 개 끝나야 다음

모델이 한 turn 에 도구를 N 개 요청해도, 클라이언트는 도구 하나가 끝난 뒤 다음 도구를
실행했다. 도구 응답 시간이 각각 2초라면 도구 3개는 대략 6초가 걸린다.

이 파일에서는 도구 실행 방식을 다음처럼 바꾼다:

    results = await asyncio.gather(*[
        call_tool_with_log(session, tu) for tu in tool_uses
    ])                                                      # N 개를 동시에 시작

그러면 도구 3개가 있어도 전체 시간은 대략 max(t1, t2, t3), 즉 가장 늦게 끝난
도구의 시간에 가까워진다.

왜 효과가 있나 — async 의 핵심:
  asyncio 는 한 스레드 안에서 동작하는 협력적 동시성이다. 멀티스레드 병렬 실행은 아니다.
  하지만 도구 호출은 대부분 I/O 대기다. RPC 를 보내고 응답을 기다리는 시간이 길다.
  task A 가 await 지점에서 양보하면 task B 가 그 사이에 진행된다.
  그래서 I/O bound 작업에서는 사용자가 체감하는 응답 시간이 줄어든다.

체감 시연:
  질문> GSC 와 NUR 학과의 상위 5명을 각각 알려줘
    → 모델이 top_students(GSC), top_students(NUR) 두 도구를 한 turn 에 호출
    → 로그에 ">>" 두 줄이 연달아 출력된다. 두 도구를 거의 동시에 시작했다는 뜻이다.
    → "<<" 로그는 완료된 순서대로 도착하므로 시작 순서와 다를 수 있다.
    → "** 병렬 종합: 2개 도구, 0.18s" (순차였다면 약 0.35s)

08 → 09 차이:
  - run_turns 의 도구 실행 블록을 for-await 순차 실행에서 asyncio.gather 로 바꾼다.
  - 총 소요 시간을 측정해 병렬 실행 효과를 눈으로 확인한다.
  - call_tool_with_log() 로 완료 시점 로그를 찍어 시작 순서와 완료 순서가 다를 수 있음을 보여 준다.

설계 노트:
  - gather 는 결과를 입력 순서대로 돌려준다. 따라서 tool_use 와 zip 으로 묶어도 안전하다.
    tool_use_id 짝짓기가 깨지면 Anthropic API 가 400 오류를 반환할 수 있다.
  - call_tool_safe 가 모든 예외를 (text, True) 로 잡으므로 한 도구 실패가
    다른 도구를 중단시키지 않는다. 그래서 gather 의 return_exceptions 옵션은 쓰지 않는다.
  - 도구가 1개뿐인 turn 에서는 속도 이점이 없다. gather 가 1개짜리 리스트로 동작할 뿐이다.

실행:
  python agent/09_parallel_tools.py
  질문> GSC 와 NUR 학과의 상위 5명을 각각 알려줘
  질문> 이름에 '김' 들어가는 학생 찾고, 동시에 학과별 통계도 보여줘
"""
import asyncio
import os
import platform
import sys
import time
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
TOOL_TIMEOUT_S = 30.0
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

    세 가지 실패 경로를 모두 (text, True) 형태로 바꾼다.
        (1) 타임아웃        : asyncio.TimeoutError
        (2) RPC 자체 실패    : 그 외 예외 (서버 죽음, 연결 끊김 등)
        (3) 도구의 isError  : RPC 는 성공했지만 서버가 실패를 표시
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
        return f"[RPC 에러] {type(e).__name__}: {e}", True

    text = extract_text_from_mcp_result(tc.content)
    return text, bool(tc.isError)


async def call_tool_with_log(session, tu) -> tuple[str, bool]:
    """call_tool_safe 에 완료 시점 로그를 더한 wrapper 함수.

    gather 안에서 각 task 가 끝나는 시점에 로그를 찍는다. 이를 통해 학생은
    완료 순서가 시작 순서와 다를 수 있음을 눈으로 확인할 수 있다.
    순차 실행에서는 시작 순서와 완료 순서가 같아서 이 차이가 보이지 않는다.
    """
    t0 = time.perf_counter()
    text, is_error = await call_tool_safe(session, tu.name, tu.input)
    dt = time.perf_counter() - t0
    marker = "  [is_error]" if is_error else ""
    log("<<", f"  완료: {tu.name} ({len(text)} 자, {dt:.2f}s){marker}")
    return text, is_error


async def run_turns(session, anthropic, tools_for_claude, messages) -> str:
    """multi-turn 루프. 08 과 같지만 도구 실행만 동시에 처리한다.

    stop_reason 분기는 08 과 같다. 변경된 부분은 tool_use 블록 안의 도구 실행 방식뿐이다.
    """
    for turn in range(1, MAX_TURNS + 1):
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

        if stop == "end_turn":
            messages.append({"role": "assistant", "content": response.content})
            return "".join(b.text for b in response.content if b.type == "text")

        if stop == "max_tokens":
            messages.append({"role": "assistant", "content": response.content})
            answer = "".join(b.text for b in response.content if b.type == "text")
            return f"{answer}\n\n[!] max_tokens 도달 — 답변이 도중에 잘렸습니다."

        if stop == "refusal":
            messages.append({"role": "assistant", "content": response.content})
            return "[X] 모델이 응답을 거부했습니다 (refusal)."

        if stop != "tool_use":
            return f"[?] 예상치 못한 stop_reason={stop!r} — 응답 처리를 중단합니다."

        # ── 변경 지점: 도구 실행을 순차(for-await)에서 동시(asyncio.gather)로 바꾼다.
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # 시작 로그는 도구를 시작한 순서대로 먼저 출력한다.
        for tu in tool_uses:
            log(">>", f"  MCP tools/call: {tu.name}({tu.input})")

        # 동시 실행: 각 task 는 I/O 대기 지점에서 제어권을 양보하며 번갈아 진행된다.
        # gather 는 입력 순서대로 결과를 모아 주므로 tool_use 와 zip 으로 묶어도 안전하다.
        t0 = time.perf_counter()
        results = await asyncio.gather(*[
            call_tool_with_log(session, tu) for tu in tool_uses
        ])
        elapsed = time.perf_counter() - t0
        log("**", f"  병렬 종합: {len(tool_uses)}개 도구, 총 {elapsed:.2f}s")

        tool_results = []
        for tu, (text, is_error) in zip(tool_uses, results):
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": [{"type": "text", "text": text}],
                "is_error": is_error,
            })
        # ── 변경 지점 끝 ────────────────────────────────────────────

        messages.append({"role": "user", "content": tool_results})

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

            print("\n=== 대화 시작 (병렬 도구 호출) ===  (/reset 초기화, /quit 종료)")
            print("  체감 시연 질문:")
            print("    'GSC 와 NUR 학과의 상위 5명을 각각 알려줘'")
            print("       → top_students(GSC), top_students(NUR) 동시 호출 유도")
            print("    '이름에 김 들어가는 학생 찾고, 동시에 학과별 통계도'")
            print("       → search_students + department_stats 동시")
            print("    'GSC 학과 평균 GPA 와 상위 3명을 한 번에'")
            print("       → department_stats + top_students(GSC) 동시")
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
