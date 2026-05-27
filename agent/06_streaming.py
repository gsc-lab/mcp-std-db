"""
Stage 3 — 스트리밍 출력 REPL (SDK UX 레이어)

01~05 로 MCP primitive 트리오(Tool/Resource/Prompt)는 다 다뤘다. 이 파일은 새 MCP
개념을 더하지 않는다 — 05 와 *MCP 흐름은 100% 동일* 하고, 바뀌는 건 출력 방식 하나:
응답을 통째로 기다렸다 찍던 걸, 토큰이 도착하는 대로 실시간으로 흘려보낸다.

★ 주의 — 이건 MCP 가 아니라 Anthropic SDK 의 UX 기능이다.
  prompts/get, tools/call, resources/read 같은 MCP wire 흐름과는 무관하다.
  스트리밍은 "모델 텍스트를 화면에 어떻게 그리느냐" 의 문제일 뿐.

05 → 06 차이 (run_turns 한 곳뿐):
  - messages.create()  (응답 전체 대기 후 반환)
      → messages.stream()  (async with 컨텍스트)
  - stream.text_stream     으로 텍스트 토큰을 실시간 print
  - stream.get_final_message() 로 tool_use 까지 포함된 완성 메시지를 복원
        → 복원된 객체는 create() 가 주던 response 와 동일 → 이후 루프 로직 그대로.
  - 답변이 run_turns 안에서 실시간 출력되므로, 호출부의 print(답변) 제거.
  - MAX_TOKENS 상향(2048→4096): 스트리밍 도중 max_tokens 로 잘리면 어색하니까.

왜 tool_use 루프가 그대로 사는가:
  text_stream 은 *텍스트만* 흘린다. tool_use 블록은 스트림에 안 실리고,
  get_final_message() 가 끝에서 통째로 재조립해준다. 그래서 02~05 의
  "stop_reason 검사 → 도구 실행 → 결과 누적" 구조를 손대지 않아도 된다.

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
MAX_TOKENS = 4096  # 05 는 2048. 스트리밍은 긴 답이 잘리면 어색해 상향.
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


async def run_turns(session, anthropic, tools_for_claude, messages) -> None:
    """한 질문에 대한 multi-turn 루프. 05 와 MCP 흐름 동일, 출력만 스트리밍.

    답변은 이 함수 안에서 실시간으로 print 된다 → 호출부는 결과를 다시 찍지 않는다.
    """
    for turn in range(1, MAX_TURNS + 1):
        # ── 05 와의 유일한 차이 ─────────────────────────────────────
        #   create() 대신 stream(). text_stream 으로 토큰을 실시간 출력하고,
        #   get_final_message() 로 tool_use 포함 완성 메시지를 복원한다.
        async with anthropic.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=tools_for_claude,
            messages=messages,
        ) as stream:
            streamed = False
            async for text in stream.text_stream:
                if not streamed:
                    print("\n답변> ", end="", flush=True)
                    streamed = True
                print(text, end="", flush=True)
            response = await stream.get_final_message()
        if streamed:
            print()  # 실시간 출력한 줄을 닫는다
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
                    # Prompt 가 대화를 seed 했으니 그대로 루프를 돌려 (실시간) 답변.
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
