"""
Web v3 — Tool 기반 chat + Resource 첨부 + Prompt 호출

v2 위에 서버 측 prompt 호출 기능을 추가한다. Claude Desktop 슬래시 메뉴와 동등한 흐름.

라우트:
  GET  /                : index.html
  GET  /api/resources   : 정적 Resources + Templates 목록 (v2 와 동일)
  GET  /api/prompts     : Prompts 목록 + 인자 명세 (v3 신설)
  POST /api/ask         : 두 가지 형태 (상호 배타) → {"answer", "rounds", "events"}
                          - {question, attach}  (v2 방식)
                          - {prompt: {name, args}}  (v3 방식)
  GET  /api/health      : 헬스체크

v2 → v3 차이:
  - prompt_message_to_anthropic 헬퍼 추가 (PromptMessage → Anthropic message)
  - list_server_prompts 함수 추가
  - /api/prompts 라우트 추가
  - /api/ask 가 prompt 분기 처리
  - 다중 호출 루프 본체는 _run_multi_turn 으로 추출해 두 흐름이 공유
  - 다중 호출 루프 자체는 v1/v2 와 동일

실행:
  python web/v3_prompts/app.py
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
    """MCP resources/read 응답에서 텍스트만 추출."""
    parts = []
    for c in contents:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(repr(c))
    return "\n".join(parts)


def prompt_message_to_anthropic(msg) -> dict:
    """MCP PromptMessage → Anthropic API 의 message 형식.

    PromptMessage.content 는 단일 ContentBlock (TextContent | EmbeddedResource | ...).
    Anthropic message.content 는 블록 리스트이므로 1-원소 리스트로 감싼다.
    EmbeddedResource 는 v2 와 동일한 <resource uri="..."> 형태로 변환.
    """
    c = msg.content
    if c.type == "text":
        blocks = [{"type": "text", "text": c.text}]
    elif c.type == "resource":
        r = c.resource
        if hasattr(r, "text"):
            blocks = [{
                "type": "text",
                "text": f"<resource uri=\"{r.uri}\">\n{r.text}\n</resource>",
            }]
        else:
            blocks = [{"type": "text", "text": repr(r)}]
    else:
        blocks = [{"type": "text", "text": repr(c)}]
    return {"role": msg.role, "content": blocks}


def server_params() -> StdioServerParameters:
    """MCP 서버 실행 파라미터."""
    return StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )


# ────────────────────────────────────────────────────────────────────
# 비동기 로직 — 디스커버리
# ────────────────────────────────────────────────────────────────────

async def list_server_resources() -> dict:
    """정적 Resources + Resource Templates 목록 (v2 와 동일)."""
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_resources()
            tpl = await session.list_resource_templates()
            return {
                "static": [
                    {"uri": r.uri, "name": r.name,
                     "description": r.description or "", "mimeType": r.mimeType or ""}
                    for r in res.resources
                ],
                "templates": [
                    {"uriTemplate": t.uriTemplate, "name": t.name,
                     "description": t.description or "", "mimeType": t.mimeType or ""}
                    for t in tpl.resourceTemplates
                ],
            }


async def list_server_prompts() -> dict:
    """Prompts 목록 + 인자 명세 (v3 신설)."""
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_prompts()
            return {
                "prompts": [
                    {
                        "name": p.name,
                        "description": p.description or "",
                        "arguments": [
                            {
                                "name": a.name,
                                "description": getattr(a, "description", "") or "",
                                "required": bool(getattr(a, "required", False)),
                            }
                            for a in (p.arguments or [])
                        ],
                    }
                    for p in res.prompts
                ],
            }


# ────────────────────────────────────────────────────────────────────
# 비동기 로직 — 에이전트 실행
# ────────────────────────────────────────────────────────────────────

async def run_chat(question: str, attach_uris: list[str]) -> dict:
    """질문 + 첨부 흐름 (v2 와 동일)."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            # 사용자 첨부 사전 조회
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
                    log("**", f"  [경고] {uri} 첨부 실패: {type(e).__name__}: {e}")

            first_content = attached_blocks + [{"type": "text", "text": question}]
            messages = [{"role": "user", "content": first_content}]
            log(">>", f"질문: {question!r} (첨부 {len(attached_blocks)}건)")

            return await _run_multi_turn(session, tools_for_claude, messages, log, events)


async def run_chat_with_prompt(prompt_name: str, prompt_args: dict) -> dict:
    """Prompt 호출 흐름 (v3 신설).

    prompts/get 으로 서버가 만든 메시지 묶음을 받아 그대로 첫 대화 상태로 사용.
    이후 다중 호출 루프는 question 흐름과 공유.
    """
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", f"MCP 서버 시작: {SERVER_PATH}")
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            # 서버 prompt 호출 — 슬래시 메뉴 동작의 등가물
            log(">>", f"MCP prompts/get: {prompt_name}({prompt_args})")
            try:
                gp = await session.get_prompt(prompt_name, prompt_args)
            except Exception as e:
                log("**", f"  [경고] prompt 호출 실패: {type(e).__name__}: {e}")
                return {
                    "answer": f"[X] prompt '{prompt_name}' 호출 실패: {e}",
                    "rounds": 0,
                    "events": events,
                }
            log("<<", f"PromptMessage {len(gp.messages)}개 수신")
            if gp.description:
                log("**", f"  prompt 설명: {gp.description}")

            # PromptMessage 묶음 → Anthropic messages 변환 → 첫 대화 상태
            messages = [prompt_message_to_anthropic(m) for m in gp.messages]
            for i, m in enumerate(messages, 1):
                first_text = m["content"][0]["text"] if m["content"] else ""
                preview = first_text[:80].replace("\n", " ")
                log("**", f"  [{i}] role={m['role']}  '{preview}...'")

            return await _run_multi_turn(session, tools_for_claude, messages, log, events)


async def _run_multi_turn(session, tools_for_claude, messages, log, events) -> dict:
    """v1/v2 와 동일한 다중 호출 루프. 두 흐름이 공유."""
    client = AsyncAnthropic()
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
    try:
        return jsonify(asyncio.run(list_server_resources()))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/prompts")
def api_prompts():
    """v3 신설."""
    try:
        return jsonify(asyncio.run(list_server_prompts()))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """질문+첨부 흐름 OR Prompt 호출 흐름 (상호 배타)."""
    data = request.get_json(silent=True) or {}
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY 가 .env 에 없습니다."}), 500

    prompt = data.get("prompt")
    if prompt:
        # Prompt 호출 분기
        name = (prompt.get("name") or "").strip()
        args = prompt.get("args") or {}
        if not name:
            return jsonify({"error": "prompt.name 이 비어있습니다."}), 400
        if not isinstance(args, dict):
            return jsonify({"error": "prompt.args 는 객체(dict) 여야 합니다."}), 400
        try:
            return jsonify(asyncio.run(run_chat_with_prompt(name, args)))
        except Exception as e:
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    # 질문+첨부 분기 (v2 와 동일)
    question = (data.get("question") or "").strip()
    attach = data.get("attach") or []
    if not isinstance(attach, list):
        return jsonify({"error": "attach 는 리스트여야 합니다."}), 400
    attach = [str(u).strip() for u in attach if str(u).strip()]
    if not question:
        return jsonify({"error": "question 또는 prompt 중 하나는 필수입니다."}), 400

    try:
        return jsonify(asyncio.run(run_chat(question, attach)))
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
