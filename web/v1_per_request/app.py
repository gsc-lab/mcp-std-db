"""
Web v1 — 요청마다 새 MCP 세션을 여는 FastAPI 예제

학습용으로 단순화한 버전이다. /api/ask 요청이 들어올 때마다 MCP 서버를 새로 실행한다.
실서비스에서는 비효율적이지만, 연결부터 도구 호출까지의 흐름이 잘 보여 학습에 적합하다.

라우트:
  GET  /                : index.html
  GET  /api/health      : 헬스체크
  GET  /api/resources   : 정적 Resources + Resource Templates 목록
  GET  /api/prompts     : Prompts 목록 + 인자 명세
  POST /api/ask         : 두 가지 입력 중 하나를 처리 → {answer, rounds, events}
                          - {question, attach: [uri, ...]}    질문 + 첨부 자료
                          - {prompt: {name, args}}            서버 prompt 호출

학습 포인트:
  - FastAPI 의 async def 라우트에서는 await 를 직접 사용할 수 있다.
  - MCP 클라이언트가 비동기라 라우트 안에서 async with 로 그대로 연결한다.
  - 매 요청마다 MCP 서버를 실행한다. 이후 단계에서는 공유 세션 구조로 발전시킬 수 있다.

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

# Windows 콘솔에서 한글이 깨지지 않도록 UTF-8 출력으로 맞춘다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def venv_python() -> Path:
    """MCP 서버 실행에 사용할 프로젝트 가상환경의 Python 경로."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def server_params() -> StdioServerParameters:
    """MCP 서버 실행 파라미터. 매 요청에서 같은 설정으로 서버를 새로 실행한다."""
    return StdioServerParameters(
        command=str(venv_python()),
        args=[str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )


# ────────────────────────────────────────────────────────────────────
# MCP 형식과 Anthropic 형식을 서로 맞춰 주는 헬퍼
# ────────────────────────────────────────────────────────────────────

def mcp_tool_to_anthropic(tool) -> dict:
    """MCP Tool 정의를 Anthropic API 의 tool 형식으로 바꾼다."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def extract_text_from_mcp_result(content_blocks) -> str:
    """MCP tools/call 응답에서 텍스트 블록을 모아 하나의 문자열로 만든다."""
    parts = []
    for b in content_blocks:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
        else:
            parts.append(repr(b))
    return "\n".join(parts)


def extract_text_from_resource_contents(contents) -> str:
    """MCP resources/read 응답에서 텍스트 본문을 모아 하나의 문자열로 만든다."""
    parts = []
    for c in contents:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(repr(c))
    return "\n".join(parts)


def prompt_message_to_anthropic(msg) -> dict:
    """MCP PromptMessage 를 Anthropic message 로 변환한다.

    EmbeddedResource 는 Anthropic 이 모르는 타입이므로 <resource> 마커가 붙은
    텍스트 블록으로 바꾼다.
    """
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
    # 그 외 타입은 학습용으로 단순하게 문자열 표현만 보낸다.
    return {"role": msg.role, "content": [{"type": "text", "text": repr(c)}]}


# ────────────────────────────────────────────────────────────────────
# 서버가 제공하는 Resources / Prompts 목록 조회
# ────────────────────────────────────────────────────────────────────

async def discover_resources() -> dict:
    """화면의 Resource 모달을 채우기 위한 목록을 조회한다.

    Resource 는 사용자가 고르는 자료이므로 LLM 에게 tools 로 전달하지 않는다.
    """
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_resources()
            tpl = await session.list_resource_templates()
            # r.uri / t.uriTemplate 은 AnyUrl 객체라 JSON 으로 보내기 전에 문자열로 바꾼다.
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
    """화면의 Prompt 모달을 채우기 위한 Prompt 목록과 인자 명세를 조회한다."""
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
# 에이전트 실행 — 두 진입점이 같은 multi-turn 루프를 사용한다.
# ────────────────────────────────────────────────────────────────────

async def run_chat(question: str, attach_uris: list[str]) -> dict:
    """자연어 질문과 사용자가 첨부한 Resource 를 함께 처리한다."""
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

            # 사용자가 첨부한 자료를 먼저 읽고, 첫 user 메시지에 참고 자료로 포함한다.
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
    """서버 Prompt 를 실행하고, prompts/get 결과를 첫 대화 상태로 사용한다."""
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
    """도구를 여러 번 호출할 수 있는 공통 루프. 종료 조건은 LLM 의 stop_reason 이다."""
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
            # NOTE: 실제 구현에서는 tc.isError 도 확인해야 한다.
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
# FastAPI 앱 — 라우트는 async def 로 두고 비동기 함수를 그대로 await 한다.
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
