"""
Stage 3 — capstone 대조: LangGraph 로 다시 짠 03_repl

03_repl.py 에서 *손으로 짠* 것(멀티턴 루프 + 대화 메모리)을, 프레임워크 LangGraph 의
기성품으로 대체한 버전. **기능은 03 과 동일** — 같은 MCP 서버, 같은 대화 메모리 REPL.
차이는 "내가 짠 100여 줄이 프레임워크에선 몇 줄" 이라는 *대조*.

★ 먼저 00~03 을 이해한 *다음* 에 볼 것. 본질을 알아야 프레임워크가 "마법"이 아니라
  "편의 도구" 로 보인다. 순서가 뒤집히면 (프레임워크 먼저) 에이전트가 어떻게 도는지
  영영 모른다.

03 의 손코딩 → LangGraph 기성품 매핑:
  ┌─────────────────────────────────────────────┬──────────────────────────────┐
  │ 03_repl.py (손으로)                          │ LangGraph                    │
  ├─────────────────────────────────────────────┼──────────────────────────────┤
  │ stdio_client + ClientSession + list_tools    │ MultiServerMCPClient          │
  │   + mcp_tool_to_anthropic 변환               │   .get_tools()                │
  │ run_turns 의 for 루프 (stop_reason 분기)     │ create_react_agent (내장 루프)│
  │ messages=[] 를 루프 밖에 둠 (대화 메모리)    │ MemorySaver + thread_id       │
  │ extract_text_from_mcp_result 변환            │ 어댑터가 자동                 │
  │ tool_use_id 짝짓기                           │ 프레임워크 내부               │
  └─────────────────────────────────────────────┴──────────────────────────────┘

핵심 — 대화 메모리의 위치:
  03: messages 리스트를 while 루프 *밖* 에 둠 → 상태가 거기 산다
  07: thread_id + checkpointer → 같은 thread_id 면 LangGraph 가 알아서 이어줌
  *개념은 동일* (대화 상태를 어딘가 유지), 표현만 다름.

★ MCP 서버(server/main.py)는 한 글자도 안 바뀐다 — 클라이언트 중립성의 증명.
  직접 SDK(03) 든 LangGraph(07) 든 같은 서버를 쓴다.

의존성 (requirements-dev.txt):
  langgraph, langchain-anthropic, langchain-mcp-adapters

실행:
  python agent/07_langgraph.py
"""
import asyncio
import os
import platform
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"


def venv_python() -> Path:
    """프로젝트 venv 의 파이썬 경로. (03 과 동일)"""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def describe_turn(messages) -> str:
    """LangGraph 가 내부에서 돈 결과(messages)를 요약 — 손코딩의 log() 에 대응.

    프레임워크가 *숨긴* 멀티턴/도구 호출을 학생이 볼 수 있도록 드러낸다.
    """
    tool_calls = []
    for m in messages:
        # AIMessage 가 도구 호출을 요청하면 .tool_calls 에 담긴다 (03 의 tool_use 에 해당)
        for tc in getattr(m, "tool_calls", None) or []:
            tool_calls.append(tc["name"])
    parts = [f"총 {len(messages)} 메시지"]
    if tool_calls:
        parts.append(f"도구 호출: {tool_calls}")
    return " · ".join(parts)


async def repl() -> None:
    # ── MCP 서버를 LangChain 도구로 로드 ──────────────────────────
    # 03 의 stdio_client + ClientSession + list_tools + 형식 변환을 이 한 블록이 대신.
    # server/main.py 는 그대로 — transport/command/args 는 우리가 늘 쓰던 값.
    client = MultiServerMCPClient({
        "student": {
            "transport": "stdio",
            "command": str(venv_python()),
            "args": [str(SERVER_PATH)],
            "env": {**os.environ, "PYTHONUTF8": "1"},
            "cwd": str(REPO_ROOT),
        }
    })
    print(f"** MCP 서버 로드: {SERVER_PATH}")
    tools = await client.get_tools()
    print(f"** 도구 목록: {[t.name for t in tools]}")

    # ── ReAct 에이전트 + 대화 메모리 ─────────────────────────────
    # create_react_agent = 02 의 멀티턴 루프 전체 (stop_reason 분기까지 내장)
    # MemorySaver(checkpointer) = 03 의 'messages 를 루프 밖에 두기' 와 같은 역할
    model = ChatAnthropic(model=MODEL, max_tokens=2048)
    agent = create_react_agent(model, tools, checkpointer=MemorySaver())

    # thread_id 가 대화 세션 식별자. 같은 id 면 LangGraph 가 이전 대화를 이어준다.
    thread_id = "session-1"
    config = {"configurable": {"thread_id": thread_id}}

    print("\n=== 대화 시작 (LangGraph) ===  (/reset 초기화, /quit 종료)")
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
            # 새 thread_id 로 바꾸면 LangGraph 가 빈 대화에서 다시 시작.
            # (03 의 messages=[] 초기화에 대응)
            thread_id = thread_id + "x"
            config = {"configurable": {"thread_id": thread_id}}
            print(f"대화를 초기화했습니다. (thread_id={thread_id})")
            continue
        if not question:
            continue

        # ── 한 번의 ainvoke 가 03 의 run_turns 전체 (멀티턴 루프) ──
        #   {"messages": [("user", q)]} 만 넘기면, 이전 대화는 checkpointer 가 자동 합류.
        result = await agent.ainvoke({"messages": [("user", question)]}, config)

        # 프레임워크가 숨긴 내부 동작을 드러내 보여줌 (대조 학습용)
        print(f"<< {describe_turn(result['messages'])}")

        # 최종 답변 = 마지막 메시지
        answer = result["messages"][-1].content
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
