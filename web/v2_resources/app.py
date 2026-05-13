"""
Web v2 — Tool 기반 chat + Resource 사용자 첨부

v1 위에 자료 첨부 기능을 추가한다. 사용자가 모달에서 URI 를 선택해 첨부하면
백엔드가 resources/read 로 미리 조회한 후, 첫 user 메시지의 content 블록으로
포함시킨다.

라우트:
  GET  /                : index.html
  GET  /api/resources   : 정적 Resources + Templates 목록 (모달 채우기용, v2 신설)
  POST /api/ask         : {"question", "attach": [uri, ...]} → {"answer", "rounds", "events"}
  GET  /api/health      : 헬스체크

v1 → v2 차이:
  - extract_text_from_resource_contents 헬퍼 추가 (Resource 응답 추출용)
  - /api/resources 라우트 추가
  - /api/ask 가 attach 배열을 받음
  - 다중 호출 루프 진입 전에 attach URI 들을 resources/read 로 조회
  - 첫 user 메시지 = [resource 블록들] + [질문]
  - 루프 자체는 v1 과 동일

실행:
  python web/v2_resources/app.py
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


def extract_text_from_resource_contents(contents) -> str:
    """MCP resources/read 응답에서 텍스트만 추출.

    Resource 응답 타입:
      - TextResourceContents (uri, mimeType, text)
      - BlobResourceContents (uri, mimeType, blob)
    학습용은 텍스트만 단순 처리.
    """
    parts = []
    for c in contents:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(repr(c))
    return "\n".join(parts)


def server_params() -> StdioServerParameters:
    """MCP 서버 실행 파라미터."""
    return StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )


# ────────────────────────────────────────────────────────────────────
# 비동기 로직
# ────────────────────────────────────────────────────────────────────

async def list_server_resources() -> dict:
    """정적 Resources 와 Resource Templates 목록.

    이 결과는 모달 표시용. LLM 에게 노출되는 tools_for_claude 에는 포함되지 않는다
    (Resource 의 선택권은 사용자에게 있음).
    """
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_resources()
            tpl = await session.list_resource_templates()
            return {
                "static": [
                    {
                        "uri": r.uri,
                        "name": r.name,
                        "description": r.description or "",
                        "mimeType": r.mimeType or "",
                    }
                    for r in res.resources
                ],
                "templates": [
                    {
                        "uriTemplate": t.uriTemplate,
                        "name": t.name,
                        "description": t.description or "",
                        "mimeType": t.mimeType or "",
                    }
                    for t in tpl.resourceTemplates
                ],
            }


async def run_agent_for_web(question: str, attach_uris: list[str]) -> dict:
    """v1 의 흐름에 사용자 첨부 단계 추가."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    # 1) MCP 서버 시작 + 도구 목록
    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            # 2) 사용자 첨부 자료 사전 조회 (루프 진입 전)
            #    각 URI 를 resources/read 로 조회해 첫 user 메시지 content 블록으로 포함시킨다.
            attached_blocks: list[dict] = []
            for uri in attach_uris:
                log(">>", f"MCP resources/read (사용자 첨부): {uri}")
                try:
                    rr = await session.read_resource(uri)
                    content = extract_text_from_resource_contents(rr.contents)
                    log("<<", f"  resource 본문 ({len(content)} 자)")
                    attached_blocks.append({
                        "type": "text",
                        "text": f"<resource uri=\"{uri}\">\n{content}\n</resource>",
                    })
                except Exception as e:
                    # NOTE: 학습용 단순 처리. 실전에선 잘못된 URI/권한 에러 분기 필요.
                    log("**", f"  [경고] {uri} 첨부 실패: {type(e).__name__}: {e}")

            # 3) 첫 user 메시지 = [첨부 블록들] + [질문]
            first_content = attached_blocks + [{"type": "text", "text": question}]
            messages = [{"role": "user", "content": first_content}]
            log(">>", f"질문: {question!r} (첨부 {len(attached_blocks)}건)")

            client = AsyncAnthropic()

            # 4) 다중 호출 루프 (v1 과 동일)
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


@app.route("/api/resources")
def api_resources():
    """모달 표시용. v2 신설."""
    try:
        return jsonify(asyncio.run(list_server_resources()))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    attach = data.get("attach") or []
    if not isinstance(attach, list):
        return jsonify({"error": "attach 는 리스트여야 합니다."}), 400
    attach = [str(u).strip() for u in attach if str(u).strip()]

    if not question:
        return jsonify({"error": "question 필드가 비어있습니다."}), 400
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY 가 .env 에 없습니다."}), 500

    try:
        result = asyncio.run(run_agent_for_web(question, attach))
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
