"""
Web v3 — 대화 메모리 (Redis)

v2 (공유 MCP 세션) 위에 *대화 기억* 을 얹은 단계. 이전까지는 매 요청이 독립이라
"방금 답변에서 가장 낮은 학과는?" 같은 후속 질문을 못 알아들었다. v3 는 Redis 에
대화를 저장해 *여러 요청에 걸쳐 맥락을 유지* — 진짜 챗봇.

메모리 설계 (간결):
  - 저장소: Redis, key = conv:{session_id}, TTL 24h (--appendonly 로 영속)
  - 저장 내용: *가시 대화만* — {role, content(텍스트)} 의 user/assistant 쌍.
    도구 상호작용(tool_use/tool_result)은 그 요청 한정이라 저장 안 함 →
    트리밍 시 tool 짝이 깨질 일 없음.
  - 크기 관리: sliding window — 최근 MAX_HISTORY turn 만 유지.
  - 세션 식별: session_id (클라이언트 localStorage, 요청 본문에 동봉).
  - prompt 호출: 독립 작업이라 메모리 미적용 (일반 질문만 기억).

v2 → v3 차이:
  - lifespan 에서 Redis 연결도 함께 생성
  - load_session / save_session (Redis get/set)
  - run_chat 가 prior_history 를 받아 messages 앞에 붙임
  - /api/ask 의 question 분기가 conversation memory 사용
  - /api/reset 라우트 추가 (대화 초기화)

라우트:
  GET  /                : index.html
  GET  /api/health      : 헬스체크 (Redis 상태 포함)
  GET  /api/resources   : 정적 Resources + Templates
  GET  /api/prompts     : Prompts 목록
  POST /api/ask         : {session_id, question, attach} 또는 {prompt: {...}}
  POST /api/reset       : {session_id} → 해당 세션 대화 삭제

실행:
  docker compose up -d        # postgres + adminer + redis
  python web/v3_conversation/app.py
  → http://localhost:5000
"""
import json
import os
import platform
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
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
MAX_HISTORY = 20          # 가시 대화 turn 최대 (sliding window)
CONV_TTL = 86400          # 대화 보관 24시간
REDIS_URL = f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def venv_python() -> Path:
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )


# ────────────────────────────────────────────────────────────────────
# MCP / Anthropic 형식 변환 헬퍼 (v2 와 동일)
# ────────────────────────────────────────────────────────────────────

def mcp_tool_to_anthropic(tool) -> dict:
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.inputSchema}


def extract_text_from_mcp_result(content_blocks) -> str:
    parts = []
    for b in content_blocks:
        parts.append(b.text if getattr(b, "type", None) == "text" else repr(b))
    return "\n".join(parts)


def extract_text_from_resource_contents(contents) -> str:
    parts = []
    for c in contents:
        parts.append(c.text if hasattr(c, "text") else repr(c))
    return "\n".join(parts)


def prompt_message_to_anthropic(msg) -> dict:
    c = msg.content
    if c.type == "text":
        return {"role": msg.role, "content": [{"type": "text", "text": c.text}]}
    if c.type == "resource":
        r = c.resource
        text = f"<resource uri=\"{r.uri}\">\n{r.text}\n</resource>" if hasattr(r, "text") else repr(r)
        return {"role": msg.role, "content": [{"type": "text", "text": text}]}
    return {"role": msg.role, "content": [{"type": "text", "text": repr(c)}]}


# ────────────────────────────────────────────────────────────────────
# 대화 메모리 — Redis (v3 신설)
# ────────────────────────────────────────────────────────────────────

async def load_session(redis_client, session_id: str) -> list:
    """Redis 에서 가시 대화 복원. 없으면 빈 리스트."""
    raw = await redis_client.get(f"conv:{session_id}")
    return json.loads(raw) if raw else []


async def save_session(redis_client, session_id: str, history: list) -> None:
    """가시 대화를 Redis 에 저장 (TTL 갱신). sliding window 로 크기 제한."""
    trimmed = history[-MAX_HISTORY:]
    await redis_client.set(
        f"conv:{session_id}",
        json.dumps(trimmed, ensure_ascii=False),
        ex=CONV_TTL,
    )


# ────────────────────────────────────────────────────────────────────
# 디스커버리 (v2 와 동일)
# ────────────────────────────────────────────────────────────────────

async def discover_resources(session: ClientSession) -> dict:
    res = await session.list_resources()
    tpl = await session.list_resource_templates()
    return {
        "static": [
            {"uri": str(r.uri), "name": r.name, "description": r.description or "", "mimeType": r.mimeType or ""}
            for r in res.resources
        ],
        "templates": [
            {"uriTemplate": str(t.uriTemplate), "name": t.name, "description": t.description or "", "mimeType": t.mimeType or ""}
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
                    {"name": a.name, "description": getattr(a, "description", "") or "", "required": bool(getattr(a, "required", False))}
                    for a in (p.arguments or [])
                ],
            }
            for p in res.prompts
        ],
    }


# ────────────────────────────────────────────────────────────────────
# 에이전트 실행
# ────────────────────────────────────────────────────────────────────

async def run_chat(session, anthropic, prior_history: list, question: str, attach_uris: list[str]) -> dict:
    """질문 + 첨부 흐름. prior_history(이전 가시 대화)를 messages 앞에 붙여 맥락 유지."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", f"기존 MCP 세션 재사용 / 이전 대화 {len(prior_history)//2} turn 복원")

    tools_result = await session.list_tools()
    tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
    log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

    # 이전 가시 대화(텍스트) 를 messages 의 앞부분으로
    messages = list(prior_history)

    # 사용자 첨부 사전 조회
    attached_blocks: list[dict] = []
    for uri in attach_uris:
        log(">>", f"MCP resources/read (사용자 첨부): {uri}")
        try:
            rr = await session.read_resource(uri)
            content = extract_text_from_resource_contents(rr.contents)
            log("<<", f"  resource 본문 ({len(content)} 자)")
            attached_blocks.append({"type": "text", "text": f"<resource uri=\"{uri}\">\n{content}\n</resource>"})
        except Exception as e:
            log("**", f"  [경고] {uri} 첨부 실패: {type(e).__name__}: {e}")

    messages.append({"role": "user", "content": attached_blocks + [{"type": "text", "text": question}]})
    log(">>", f"질문: {question!r} (첨부 {len(attached_blocks)}건)")

    return await _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events)


async def run_chat_with_prompt(session, anthropic, prompt_name: str, prompt_args: dict) -> dict:
    """Prompt 호출 흐름. 독립 작업이라 대화 메모리 미적용."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", "기존 MCP 세션 재사용 (prompt 호출, 메모리 미적용)")

    tools_result = await session.list_tools()
    tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
    log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

    log(">>", f"MCP prompts/get: {prompt_name}({prompt_args})")
    try:
        gp = await session.get_prompt(prompt_name, prompt_args)
    except Exception as e:
        log("**", f"  [경고] prompt 호출 실패: {type(e).__name__}: {e}")
        return {"answer": f"[X] prompt '{prompt_name}' 호출 실패: {e}", "rounds": 0, "events": events}
    log("<<", f"PromptMessage {len(gp.messages)}개 수신")

    messages = [prompt_message_to_anthropic(m) for m in gp.messages]
    return await _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events)


async def _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events) -> dict:
    """다중 호출 루프 (v1/v2 와 동일)."""
    for turn in range(1, MAX_TURNS + 1):
        response = await anthropic.messages.create(
            model=MODEL, max_tokens=2048, tools=tools_for_claude, messages=messages,
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
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": [{"type": "text", "text": result_text}]})

        messages.append({"role": "user", "content": tool_results})
        log("**", f"  다음 라운드 (대화 이력 {len(messages)} 메시지)")

    return {"answer": f"[!] {MAX_TURNS} 라운드 한도 도달.", "rounds": MAX_TURNS, "events": events}


# ════════════════════════════════════════════════════════════════════
# FastAPI lifespan — MCP 세션 + Redis 연결을 앱 시작 시 1회 생성
# ════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[i] MCP 세션 + Redis 초기화 중...")
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            app.state.mcp_session = session
            app.state.anthropic = AsyncAnthropic()
            app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            print(f"[i] 초기화 완료 — MCP 세션 재사용, Redis={REDIS_URL}")
            try:
                yield
            finally:
                print("[i] 정리 중...")
                await app.state.redis.aclose()
    print("[i] 정리 완료")


app = FastAPI(title="student-mcp · web v3 (대화 메모리)", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health(request: Request):
    redis_ok = False
    try:
        redis_ok = await request.app.state.redis.ping()
    except Exception:
        pass
    return {
        "ok": request.app.state.mcp_session is not None,
        "anthropic_key_loaded": bool(os.getenv("ANTHROPIC_API_KEY")),
        "redis_ok": bool(redis_ok),
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


@app.post("/api/reset")
async def api_reset(request: Request):
    """해당 세션의 대화 기록 삭제 (새 대화)."""
    data = await request.json() if await request.body() else {}
    session_id = (data.get("session_id") or "").strip()
    if session_id:
        await request.app.state.redis.delete(f"conv:{session_id}")
    return {"ok": True}


@app.post("/api/ask")
async def api_ask(request: Request):
    data = await request.json() if await request.body() else {}
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY 가 .env 에 없습니다.")

    session = request.app.state.mcp_session
    anthropic = request.app.state.anthropic
    redis_client = request.app.state.redis

    # prompt 호출 — 독립 작업, 메모리 미적용
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

    # 질문 — 대화 메모리 적용
    session_id = (data.get("session_id") or "").strip()
    question = (data.get("question") or "").strip()
    attach = data.get("attach") or []
    if not isinstance(attach, list):
        raise HTTPException(status_code=400, detail="attach 는 리스트여야 합니다.")
    attach = [str(u).strip() for u in attach if str(u).strip()]
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 가 필요합니다.")
    if not question:
        raise HTTPException(status_code=400, detail="question 또는 prompt 중 하나는 필수입니다.")

    try:
        # 1) 이전 대화 복원
        history = await load_session(redis_client, session_id)
        # 2) 에이전트 실행 (history 를 맥락으로)
        result = await run_chat(session, anthropic, history, question, attach)
        # 3) 가시 대화만 저장 (질문 + 최종 답변 텍스트)
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": result["answer"]})
        await save_session(redis_client, session_id, history)
        return result
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
