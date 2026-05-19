"""
Web v1 — 요청별 MCP 세션 (FastAPI 기반)

학습용 단순화 버전. 매 /api/ask 요청마다 MCP 서버를 새로 spawn 한다.
실서비스에선 비효율적이지만 흐름이 가려지지 않아 학습 단계에 적합.

라우트:
  GET  /                : index.html
  GET  /api/health      : 헬스체크
  GET  /api/resources   : 정적 Resources + Resource Templates 목록
  GET  /api/prompts     : Prompts 목록 + 인자 명세
  POST /api/ask         : 두 가지 입력 (상호 배타) → {answer, rounds, events}
                          - {question, attach: [uri, ...]}    질문 + 첨부 자료
                          - {prompt: {name, args}}            서버 prompt 호출

학습 포인트:
  - FastAPI 의 async def 라우트 — Flask 와 달리 await 직접 사용 가능
  - MCP 클라이언트가 비동기라 라우트 안에서 그대로 async with 사용
  - 매 요청마다 MCP 서버를 spawn → v2_shared_session 에서 공유 세션으로 발전

실행:
  python web/v1_per_request/app.py
  → http://localhost:5000
"""
import os
import platform
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Windows 콘솔의 한글 출력 보장.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def venv_python() -> Path:
    """프로젝트 venv 의 파이썬 경로. MCP 서버는 이 인터프리터로 실행된다."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def server_params() -> StdioServerParameters:
    """MCP 서버 실행 파라미터. 매 요청마다 동일한 값으로 spawn."""
    return StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )


# ────────────────────────────────────────────────────────────────────
# MCP / Anthropic 형식 변환 헬퍼
# ────────────────────────────────────────────────────────────────────

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
    """MCP PromptMessage → Anthropic message. EmbeddedResource 는 <resource> 마커로."""
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
    # 그 외 타입은 학습용 단순 처리
    return {"role": msg.role, "content": [{"type": "text", "text": repr(c)}]}


# ────────────────────────────────────────────────────────────────────
# 서버 디스커버리 — Resources / Prompts 목록 조회
# ────────────────────────────────────────────────────────────────────

async def discover_resources() -> dict:
    """모달 채우기용 — 정적 Resources + Resource Templates 목록.

    LLM 의 tools_for_claude 에는 들어가지 않는다 (Resource 선택권은 사용자에게).
    """
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_resources()
            tpl = await session.list_resource_templates()
            # NOTE: r.uri / t.uriTemplate 은 pydantic.AnyUrl 객체. str() 로 변환해야 JSON 직렬화 가능.
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


async def discover_prompts() -> dict:
    """모달 채우기용 — Prompts 목록 + 인자 명세."""
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
# 에이전트 실행 — 두 가지 진입점이 공통 multi-turn 루프 사용
# ────────────────────────────────────────────────────────────────────

async def run_chat(question: str, attach_uris: list[str]) -> dict:
    """질문 + 자료 첨부 흐름."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", f"MCP 서버 시작 (요청별 spawn): {SERVER_PATH}")
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

            tools_result = await session.list_tools()
            tools_for_claude = [mcp_tool_to_anthropic(t) for t in tools_result.tools]
            log("**", f"도구 목록: {[t.name for t in tools_result.tools]}")

            # 사용자 첨부 자료를 루프 진입 전에 사전 조회 → 첫 user 메시지에 포함.
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

            anthropic = AsyncAnthropic()
            return await _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events)


async def run_chat_with_prompt(prompt_name: str, prompt_args: dict) -> dict:
    """서버 prompt 호출 흐름. prompts/get 결과를 첫 대화 상태로 사용."""
    events: list[dict] = []

    def log(direction: str, text: str) -> None:
        events.append({"direction": direction, "text": text})

    log("**", f"MCP 서버 시작 (요청별 spawn): {SERVER_PATH}")
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log("**", "MCP initialize 완료")

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

            anthropic = AsyncAnthropic()
            return await _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events)


async def _run_multi_turn(session, anthropic, tools_for_claude, messages, log, events) -> dict:
    """다중 호출 루프. 종료 조건은 LLM 의 stop_reason."""
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
            # NOTE: 실제 구현에선 tc.isError 검사 필요.
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
# FastAPI 앱 — 라우트는 async def, 비동기 함수를 그대로 await
# ────────────────────────────────────────────────────────────────────
app = FastAPI(title="student-mcp · web v1 (요청별 MCP 세션)")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "server_path": str(SERVER_PATH),
        "anthropic_key_loaded": bool(os.getenv("ANTHROPIC_API_KEY")),
    }


@app.get("/api/resources")
async def api_resources():
    try:
        return await discover_resources()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/prompts")
async def api_prompts():
    try:
        return await discover_prompts()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/api/ask")
async def api_ask(request: Request):
    data = await request.json() if await request.body() else {}
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY 가 .env 에 없습니다.")

    prompt = data.get("prompt")
    if prompt:
        name = (prompt.get("name") or "").strip()
        args = prompt.get("args") or {}
        if not name:
            raise HTTPException(status_code=400, detail="prompt.name 이 비어있습니다.")
        if not isinstance(args, dict):
            raise HTTPException(status_code=400, detail="prompt.args 는 객체(dict) 여야 합니다.")
        try:
            return await run_chat_with_prompt(name, args)
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
        return await run_chat(question, attach)
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
