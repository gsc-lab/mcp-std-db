"""
Stage 3 — 마무리 비교: 03_repl 을 LangGraph 로 다시 작성한 버전

03_repl.py 에서 직접 작성했던 멀티턴 루프와 대화 기억을 LangGraph 프레임워크의
기능으로 바꾼 버전이다. 기능은 03 과 같다. 같은 MCP 서버를 사용하고,
대화형 REPL 로 동작한다. 차이는 "직접 작성한 코드가 프레임워크에서는 어떻게
대체되는가" 를 보여 준다는 점이다.

먼저 00~03 을 이해한 다음 이 파일을 보는 것이 좋다. 기본 흐름을 알고 나면
프레임워크가 "마법"이 아니라 반복 코드를 줄여 주는 편의 도구로 보인다.

03 의 손코딩 → LangGraph 기성품 매핑:
  ┌─────────────────────────────────────────────┬──────────────────────────────┐
  │ 03_repl.py (손으로)                          │ LangGraph                    │
  ├─────────────────────────────────────────────┼──────────────────────────────┤
  │ stdio_client + ClientSession + list_tools    │ MultiServerMCPClient          │
  │   + mcp_tool_to_anthropic 변환               │   .get_tools()                │
  │ run_turns 의 for 루프 (stop_reason 분기)     │ create_react_agent (내장 루프)│
  │ messages=[] 를 루프 밖에 둠 (대화 기억)      │ MemorySaver + thread_id       │
  │ extract_text_from_mcp_result 변환            │ 어댑터가 자동                 │
  │ tool_use_id 짝짓기                           │ 프레임워크 내부               │
  └─────────────────────────────────────────────┴──────────────────────────────┘

핵심 — 대화 기억의 위치:
  03: messages 리스트를 while 루프 밖에 둔다 → 상태가 그 리스트에 남는다.
  07: thread_id + checkpointer 를 사용한다 → 같은 thread_id 면 LangGraph 가 이어 준다.
  개념은 "대화 상태를 어딘가에 유지한다" 로 같고, 표현 방식만 다르다.

MCP 서버(server/main.py)는 바꾸지 않는다. 직접 SDK 를 쓰든 LangGraph 를 쓰든
같은 서버를 사용할 수 있다는 점이 MCP 의 클라이언트 중립성을 보여 준다.

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
    """MCP 서버 실행에 사용할 프로젝트 가상환경의 Python 경로."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def describe_turn(messages) -> str:
    """LangGraph 가 내부에서 처리한 messages 를 학생이 보기 쉽게 요약한다.

    프레임워크 안쪽에서 일어난 멀티턴 처리와 도구 호출을 로그처럼 드러내는 역할이다.
    """
    tool_calls = []
    for m in messages:
        # AIMessage 가 도구 호출을 요청하면 .tool_calls 에 담긴다. 03 의 tool_use 와 같은 역할이다.
        for tc in getattr(m, "tool_calls", None) or []:
            tool_calls.append(tc["name"])
    parts = [f"총 {len(messages)} 메시지"]
    if tool_calls:
        parts.append(f"도구 호출: {tool_calls}")
    return " · ".join(parts)


async def repl() -> None:
    # ── MCP 서버를 LangChain 도구로 로드 ──────────────────────────
    # 03 의 stdio_client + ClientSession + list_tools + 형식 변환을 이 블록이 대신한다.
    # server/main.py 는 그대로 두고, 실행 방식(transport/command/args)만 지정한다.
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

    # ── ReAct 에이전트 + 대화 기억 ─────────────────────────────
    # create_react_agent 는 02 의 멀티턴 루프 전체를 맡는다.
    # MemorySaver(checkpointer)는 03 에서 messages 를 루프 밖에 둔 것과 같은 역할이다.
    model = ChatAnthropic(model=MODEL, max_tokens=2048)
    agent = create_react_agent(model, tools, checkpointer=MemorySaver())

    # thread_id 는 대화 세션 식별자다. 같은 id 를 쓰면 LangGraph 가 이전 대화를 이어 준다.
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
            # 새 thread_id 로 바꾸면 LangGraph 가 빈 대화에서 다시 시작한다.
            # 03 의 messages=[] 초기화에 해당한다.
            thread_id = thread_id + "x"
            config = {"configurable": {"thread_id": thread_id}}
            print(f"대화를 초기화했습니다. (thread_id={thread_id})")
            continue
        if not question:
            continue

        # ── 한 번의 ainvoke 가 03 의 run_turns 전체(멀티턴 루프)에 해당한다. ──
        #   현재 질문만 넘기면 이전 대화는 checkpointer 가 자동으로 합쳐 준다.
        result = await agent.ainvoke({"messages": [("user", question)]}, config)

        # 프레임워크 내부 동작을 요약해 보여 준다. 직접 구현 버전과 비교하기 위함이다.
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
