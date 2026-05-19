"""
Web v2 — 공유 MCP 세션 (FastAPI lifespan 기반)

기능은 v1 과 동일. 차이는 *MCP 세션을 앱 시작 시 1회만 열고 모든 요청이 재사용*.
FastAPI 의 lifespan 컨텍스트 매니저로 자연스럽게 처리되어 v1 대비 코드가 더 짧다.

v1 → v2 차이 (이 파일을 v1_per_request/app.py 와 diff 떠서 비교):
  - lifespan(app): 앱 시작 시 async with stdio_client + ClientSession 진입,
    yield 시점에 멈춰서 앱 종료까지 세션 유지. 종료 시 자동 정리.
  - app.state.mcp_session / app.state.anthropic 에 보관 — 모든 라우트가 공유.
  - 라우트는 request.app.state 에서 세션을 가져와 사용. 매번 spawn 없음.
  - discover_resources / discover_prompts / run_chat / run_chat_with_prompt 가
    session 인자를 외부에서 받음.

라우트는 v1 과 동일:
  GET  /                : index.html
  GET  /api/health      : 헬스체크 (세션 상태 포함)
  GET  /api/resources   : 정적 Resources + Templates
  GET  /api/prompts     : Prompts 목록 + 인자 명세
  POST /api/ask         : 질문+첨부 또는 prompt 호출

실행:
  python web/v2_shared_session/app.py
  → http://localhost:5000
"""
import os
import platform
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def venv_python() -> Path:
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def server_params() -> StdioServerParameters:
    """MCP 서버 실행 파라미터. v2 에선 단 1회만 사용 (lifespan 안에서)."""
    return StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )


# ────────────────────────────────────────────────────────────────────
# MCP / Anthropic 형식 변환 헬퍼 (v1 과 동일)
# ────────────────────────────────────────────────────────────────────

def mcp_tool_to_anthropic(tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def extract_text_from_mcp_result(content_blocks) -> str:
    parts = []
    for b in content_blocks:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
        else:
            parts.append(repr(b))
    return "\n".join(parts)


def extract_text_from_resource_contents(contents) -> str:
    parts = []
    for c in contents:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(repr(c))
    return "\n".join(parts)


def prompt_message_to_anthropic(msg) -> dict:
    c = msg.content
    if c.type == "text":
        return {"role": msg.role, "content": [{"type": "text", "text": c.text}]}
    if c.type == "resource":
        r = c.resource
        text = (
            f"<resource uri=\"{r.uri}\">\n{r.text}\n</resource>"
            if hasattr(r, "text") else repr(r)
        )
        return {"role": msg.role, "content": [{"type": "text", "text": text}]}
    return {"role": msg.role, "content": [{"type": "text", "text": repr(c)}]}


# ────────────────────────────────────────────────────────────────────
# 서버 디스커버리 — 세션을 외부에서 받음 (v1 과 차이)
# ────────────────────────────────────────────────────────────────────

async def discover_resources(session: ClientSession) -> dict:
    res = await session.list_resources()
    tpl = await session.list_resource_templates()
    return {
        "static": [
            {"uri": str(r.uri), "name": r.name,
             "description": r.description or "", "mimeType": r.mimeType or ""}
            for r in res.resources
        ],
        "templates": [
            {"uriTemplate": str(t.uriTemplate), "name": t.name,
             "description": t.description or "", "mimeType": t.mimeType or ""}
            for t in tpl.resourceTemplates
        ],
    }


async def discover_prompts(session: ClientSession) -> dict:
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
# 에이전트 실행 — session/anthropic 을 외부에서 받음 (v1 과 차이)
# ────────────────────────────────────────────────────────────────────

async def run_chat(
    session: ClientSession,
    anthropic: AsyncAnthropic,
    question: str,
    attach_uris: list[str],
) -> dict:
    """질문 + 자료 첨부 흐름."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", "기존 MCP 세션 재사용 (앱 시작 시 1회 초기화)")

    tools_result = await session.list_tools()
    tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
    log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

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

    return await _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events)


async def run_chat_with_prompt(
    session: ClientSession,
    anthropic: AsyncAnthropic,
    prompt_name: str,
    prompt_args: dict,
) -> dict:
    """서버 prompt 호출 흐름."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", "기존 MCP 세션 재사용")

    tools_result = await session.list_tools()
    tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
    log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

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

    messages = [prompt_message_to_anthropic(m) for m in gp.messages]
    for i, m in enumerate(messages, 1):
        first_text = m["content"][0]["text"] if m["content"] else ""
        preview = first_text[:80].replace("\n", " ")
        log("**", f"  [{i}] role={m['role']}  '{preview}...'")

    return await _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events)


async def _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events) -> dict:
    """다중 호출 루프. v1 의 _run_multi_turn 과 본문 동일."""
    for turn in range(1, MAX_TURNS + 1):
        response = await anthropic.messages.create(
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


# ════════════════════════════════════════════════════════════════════
# FastAPI lifespan — v2 의 핵심
#
# 앱 시작 시: stdio_client + ClientSession 을 async with 로 진입, initialize.
# yield 시점에 멈춰 앱이 살아있는 동안 대기. 모든 라우트가 같은 세션 재사용.
# 앱 종료 시: async with 들이 자동으로 정리.
# ════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[i] MCP 세션 초기화 중...")
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            app.state.mcp_session = session
            app.state.anthropic = AsyncAnthropic()
            print("[i] MCP 세션 초기화 완료 — 모든 요청이 재사용")
            yield
            # yield 이후는 앱 종료 시 실행 (정리)
            print("[i] MCP 세션 정리 중...")
    print("[i] MCP 세션 정리 완료")


app = FastAPI(title="student-mcp · web v2 (공유 MCP 세션)", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health(request: Request):
    session = getattr(request.app.state, "mcp_session", None)
    return {
        "ok": session is not None,
        "server_path": str(SERVER_PATH),
        "anthropic_key_loaded": bool(os.getenv("ANTHROPIC_API_KEY")),
        "session_active": session is not None,
    }


@app.get("/api/resources")
async def api_resources(request: Request):
    try:
        return await discover_resources(request.app.state.mcp_session)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/prompts")
async def api_prompts(request: Request):
    try:
        return await discover_prompts(request.app.state.mcp_session)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/api/ask")
async def api_ask(request: Request):
    data = await request.json() if await request.body() else {}
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY 가 .env 에 없습니다.")

    session = request.app.state.mcp_session
    anthropic = request.app.state.anthropic

    prompt = data.get("prompt")
    if prompt:
        name = (prompt.get("name") or "").strip()
        args = prompt.get("args") or {}
        if not name:
            raise HTTPException(status_code=400, detail="prompt.name 이 비어있습니다.")
        if not isinstance(args, dict):
            raise HTTPException(status_code=400, detail="prompt.args 는 객체(dict) 여야 합니다.")
        try:
            return await run_chat_with_prompt(session, anthropic, name, args)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    question = (data.get("question") or "").strip()
    attach = data.get("attach") or []
    if not isinstance(attach, list):
        raise HTTPException(status_code=400, detail="attach 는 리스트여야 합니다.")
    attach = [str(u).strip() for u in attach if str(u).strip()]
    if not question:
        raise HTTPException(status_code=400, detail="question 또는 prompt 중 하나는 필수입니다.")

    try:
        return await run_chat(session, anthropic, question, attach)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    import uvicorn
    if not STATIC_DIR.exists():
        print(f"[X] 정적 폴더가 없습니다: {STATIC_DIR}", file=sys.stderr)
        sys.exit(1)
    if not SERVER_PATH.exists():
        print(f"[X] MCP 서버 스크립트가 없습니다: {SERVER_PATH}", file=sys.stderr)
        sys.exit(1)
    print("[i] http://localhost:5000 으로 접속하세요. (Ctrl+C 종료)")
    uvicorn.run(app, host="127.0.0.1", port=5000, log_level="info")
