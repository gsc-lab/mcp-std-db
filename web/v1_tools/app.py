"""
Web v1 — Tool 기반 chat (Flask + Vanilla HTML/JS)

agent/02_multi_turn.py 의 로직을 Flask 라우트 안에 옮긴 첫 단계.
콘솔 로그는 events 배열로 모아 JSON 으로 반환, 프론트가 화면에 표시한다.

라우트:
  GET  /            : index.html
  POST /api/ask     : {"question"} → {"answer", "rounds", "events"}
  GET  /api/health  : 헬스체크

학습용 단순화:
  - 매 /api/ask 요청은 MCP 서버를 새로 실행. 한 요청 = 한 MCP 라이프사이클.
  - Flask 는 동기. 라우트 안에서 asyncio.run(...) 으로 비동기 코드를 한 번 실행.
    (Quart 같은 비동기 프레임워크는 학습 부담을 늘려서 도입하지 않음.)

실행:
  python web/v1_tools/app.py
  → http://localhost:5000
"""
import asyncio
import os
import platform
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def venv_python() -> Path:
    """프로젝트 venv 의 파이썬 경로."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def mcp_tool_to_anthropic(tool) -> dict:
    """MCP Tool 정의 → Anthropic API 의 tool 형식."""
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


async def run_agent_for_web(question: str) -> dict:
    """02_multi_turn 의 루프와 같다. 차이는 통신 로그를 stdout 이 아닌 events 배열에 모은다는 점."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

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

            # 2) 다중 호출 루프 (02_multi_turn 과 동일)
            for turn in range(1, MAX_TURNS + 1):
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=2048,
                    tools=tools_for_claude,
                    messages=messages,
                )
                log("<<", f"turn {turn} — stop_reason={response.stop_reason}")

                if response.stop_reason != "tool_use":
                    answer = "".join(b.text for b in response.content if b.type == "text")
                    return {"answer": answer, "rounds": turn, "events": events}

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
                log("**", f"  다음 라운드 (대화 이력 {len(messages)} 메시지)")

            return {
                "answer": f"[!] {MAX_TURNS} 라운드 한도 도달 — 모델이 도구 호출을 멈추지 못함.",
                "rounds": MAX_TURNS,
                "events": events,
            }


# ────────────────────────────────────────────────────────────────────
# Flask 앱
# ────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "server_path": str(SERVER_PATH),
        "anthropic_key_loaded": bool(os.getenv("ANTHROPIC_API_KEY")),
    })


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question 필드가 비어있습니다."}), 400
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY 가 .env 에 없습니다."}), 500

    try:
        # Flask 동기 라우트에서 비동기 함수 실행
        result = asyncio.run(run_agent_for_web(question))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


if __name__ == "__main__":
    if not STATIC_DIR.exists():
        print(f"[X] 정적 폴더가 없습니다: {STATIC_DIR}", file=sys.stderr)
        sys.exit(1)
    if not SERVER_PATH.exists():
        print(f"[X] MCP 서버 스크립트가 없습니다: {SERVER_PATH}", file=sys.stderr)
        sys.exit(1)
    print("[i] http://localhost:5000 으로 접속하세요. (Ctrl+C 종료)")
    app.run(host="127.0.0.1", port=5000, debug=True)
