"""
Stage 3 — Resource 를 첨부하는 REPL (앱/사용자가 고르는 컨텍스트)

01~03 은 Tool 만 사용했다. Tool 은 모델이 "이 데이터가 필요하다" 고 판단할 때
호출한다(model-controlled). 이 파일에서는 MCP 의 두 번째 primitive 인 Resource 를 쓴다.

핵심 — Tool 과 Resource 의 차이는 "무엇을 할 수 있느냐" 보다 "누가 선택하느냐" 다:
  - Tool      = 모델이 turn 중에 호출한다. (RPC: tools/call)
  - Resource  = URI 로 식별되는 읽기 전용 자료를 사람/앱이 골라
                컨텍스트에 미리 넣는다. (RPC: resources/read)

Claude Desktop 에는 Resource 를 고르는 "@첨부" UI 가 있다. CLI 에는 그런 UI 가 없으므로
여기서는 /attach 명령으로 같은 역할을 흉내 낸다. 사용자가 직접 자료를 고르고,
첨부된 자료는 매 turn system 프롬프트로 주입된다.

03 → 04 차이:
  - run_turns() 에 system 인자 추가 — 첨부된 Resource 내용이 여기로 들어간다.
  - /attach <uri>   서버에서 resources/read → attached 에 저장 (system 에 주입)
  - /detach <uri>   첨부 해제
  - /ctx            현재 첨부된 자료 목록
  - /resources      서버가 노출한 Resource 목록 + 템플릿 목록 조회
  - 대화 메모리(messages 누적)는 03 그대로 유지.

체감 포인트 — 자료를 붙이면 도구 호출이 줄어든다:
  (붙이기 전)  질문> 학과 다 알려줘
               → 모델이 도구를 호출할 수도 있다 (로그에 >> tools/call 표시)
  /attach departments://all
  (붙인 후)    질문> 학과 다 알려줘
               → 자료가 이미 system 에 있으니 도구 없이 답할 수 있다.
  로그 차이를 보면 "앱이 컨텍스트를 미리 제공하면 모델 행동이 달라진다" 는 점이 보인다.

템플릿 자료:
  departments://all          정적 — /resources 목록에 뜬다.
  courses://{department_code} 템플릿 — /attach courses://GSC 처럼 URI 를 직접 채운다.
  students://{student_no}     템플릿 — 학번은 목록에 직접 뜨지 않는다.
                              알고 있는 학번으로 /attach students://20240001.

실행:
  python agent/04_resources.py
"""
import asyncio
import json
import os
import platform
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"


def venv_python() -> Path:
    """MCP 서버 실행에 사용할 프로젝트 가상환경의 Python 경로."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def log(direction: str, text: str) -> None:
    """통신 흐름을 보기 쉽게 출력한다. >> 보냄, << 받음, ** 상태."""
    print(f"{direction} {text}", flush=True)


def mcp_tool_to_anthropic(tool) -> dict:
    """MCP Tool 정의를 Anthropic API 가 요구하는 tool 형식으로 바꾼다.

    입력 — tool: mcp.types.Tool
        .name        : str
        .description : str | None
        .inputSchema : dict        # JSON Schema

    반환 — dict (Anthropic messages.create 의 `tools` 목록에 들어갈 원소)
        {"name": str, "description": str, "input_schema": dict}
    """
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def extract_text_from_mcp_result(content_blocks) -> str:
    """MCP tools/call 응답의 content 블록들을 하나의 문자열로 합친다.

    입력 — content_blocks: list[ContentBlock]   (CallToolResult.content)
        TextContent       : .type="text",     .text: str
        ImageContent      : .type="image",    .data, .mimeType  (여기서는 repr 처리)
        EmbeddedResource  : .type="resource", .resource         (여기서는 repr 처리)
        AudioContent / ResourceLink: 같은 방식으로 repr 처리

    반환 — TextContent.text 는 줄바꿈으로 연결하고, 나머지 타입은 repr 로 표현한 문자열.
    """
    parts = []
    for b in content_blocks:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
        else:
            parts.append(repr(b))
    return "\n".join(parts)


def extract_resource_text(result) -> str:
    """MCP resources/read 응답의 contents 를 하나의 문자열로 합친다.

    contents 는 TextResourceContents(.text) | BlobResourceContents(.blob) 의 리스트.
    이 서버는 application/json 텍스트를 반환하므로 .text 를 우선 사용한다.
    """
    parts = []
    for c in result.contents:
        text = getattr(c, "text", None)
        parts.append(text if text is not None else repr(c))
    return "\n".join(parts)


def build_system(attached: dict[str, str]) -> str | None:
    """첨부된 Resource 들을 하나의 system 프롬프트 문자열로 합친다.

    이것이 application-controlled context 의 실제 모습이다. 대화(messages)와 별도로
    앱이 골라 넣은 참고 자료 블록이며, 첨부가 없으면 system 자체를 보내지 않는다.
    """
    if not attached:
        return None
    blocks = ["다음은 사용자가 첨부한 참고 자료다. 질문에 적극 활용하라.", ""]
    for uri, text in attached.items():
        blocks.append(f"### {uri}")
        blocks.append(text)
        blocks.append("")
    return "\n".join(blocks)


async def run_turns(session, anthropic, tools_for_claude, messages, system) -> str:
    """질문 하나를 처리하는 multi-turn 루프. 03 과 같고 system 인자만 추가됐다.

    system 에는 build_system() 이 만든 첨부 자료 블록이 들어온다. 필요한 자료가
    이미 컨텍스트에 있으면 모델은 같은 데이터를 얻기 위해 도구를 부르지 않아도 된다.
    """
    for turn in range(1, MAX_TURNS + 1):
        kwargs = dict(
            model=MODEL,
            max_tokens=2048,
            tools=tools_for_claude,
            messages=messages,
        )
        if system:
            kwargs["system"] = system

        response = await anthropic.messages.create(**kwargs)
        log("<<", f"turn {turn} — stop_reason={response.stop_reason}")

        if response.stop_reason != "tool_use":
            answer = "".join(b.text for b in response.content if b.type == "text")
            messages.append({"role": "assistant", "content": response.content})
            return answer

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

    return f"[!] {MAX_TURNS} 라운드 한도 도달."


async def cmd_resources(session) -> None:
    """서버가 제공하는 Resource 목록을 조회한다.

    Resource 는 URI 로 식별되는 읽기 전용 자료이며, Tool 과는 별도 목록이다.
    정적 Resource 는 완성된 URI 로 제공되고, 템플릿 Resource 는 사용자가 `{param}`
    자리를 채워 /attach 한다. 실제 본문 읽기는 cmd_attach() 에서 처리한다.

    반환 — await session.list_resources() → ListResourcesResult
        .resources: list[Resource]
            .uri         : AnyUrl
            .name        : str
            .description : str | None
            .mimeType    : str | None

    반환 — await session.list_resource_templates() → ListResourceTemplatesResult
        .resourceTemplates: list[ResourceTemplate]
            .uriTemplate : str        # 예: "courses://{department_code}"
            .name        : str
            .description : str | None
            .mimeType    : str | None
    """
    log(">>", "resources/list")
    res = await session.list_resources()
    print("\n[정적 Resource]")
    for r in res.resources:
        print(f"  {r.uri}    {r.name}")

    log(">>", "resources/templates/list")
    tmpl = await session.list_resource_templates()
    print("[템플릿 Resource]  (URI 를 직접 채워 /attach)")
    for t in tmpl.resourceTemplates:
        print(f"  {t.uriTemplate}    {t.name}")


async def cmd_attach(session, attached: dict[str, str], uri: str) -> None:
    """Resource 본문을 읽어 attached dict 에 저장한다.

    이후 매 turn build_system() 이 attached 내용을 system 프롬프트에 넣는다.
    즉, 사용자가 고른 자료가 모델의 참고 컨텍스트가 된다.

    반환 — await session.read_resource(uri) → ReadResourceResult
        .contents: list[TextResourceContents | BlobResourceContents]
            TextResourceContents (텍스트 자료):
                .uri      : AnyUrl
                .mimeType : str | None
                .text     : str
            BlobResourceContents (바이너리 자료):
                .uri      : AnyUrl
                .mimeType : str | None
                .blob     : str (base64)

    NOTE: 서버가 200 OK 와 함께 {"error": ...} JSON 을 반환하는 경우가 있다.
    이때는 첨부하지 않아야 에러 메시지가 참고 자료처럼 주입되는 일을 막을 수 있다.
    """
    log(">>", f"resources/read: {uri}")
    try:
        result = await session.read_resource(AnyUrl(uri))
    except Exception as e:
        log("**", f"읽기 실패: {e}")
        return
    text = extract_resource_text(result)

    # 예외는 없었지만 서버가 payload 로 에러를 알려주는 경우가 있다(예: 없는 학번).
    # 이 JSON 을 첨부하면 에러 메시지가 참고 자료로 들어가므로 미리 걸러낸다.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and "error" in payload:
        log("**", f"첨부 안 함 — 서버 에러: {payload['error']}")
        return

    attached[uri] = text
    log("<<", f"resources/read 결과 ({len(text)} 자) — 첨부됨")


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

            client = AsyncAnthropic()

            # 대화 이력은 03 과 동일하게 turn 간 누적한다.
            messages: list[dict] = []

            # 첨부된 Resource: 사용자가 /attach 로 고른 {uri: 본문} 자료.
            # 매 turn build_system() 을 통해 system 프롬프트에 주입된다.
            attached: dict[str, str] = {}

            print("\n=== 대화 시작 ===")
            print("  /resources         서버가 노출한 Resource 목록")
            print("  /attach <uri>      Resource 를 컨텍스트에 첨부 (예: /attach departments://all)")
            print("  /detach <uri>      첨부 해제")
            print("  /ctx               현재 첨부된 자료")
            print("  /reset             대화 초기화 (첨부는 유지)")
            print("  /quit              종료")
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
                    print("대화를 초기화했습니다. (첨부 자료는 유지)")
                    continue
                if line == "/resources":
                    await cmd_resources(session)
                    continue
                if line == "/ctx":
                    if not attached:
                        print("첨부된 자료가 없습니다.")
                    else:
                        print("\n[첨부된 자료]")
                        for uri, text in attached.items():
                            print(f"  {uri}  ({len(text)} 자)")
                    continue
                if line.startswith("/attach"):
                    parts = line.split(maxsplit=1)
                    if len(parts) < 2:
                        print("사용법: /attach <uri>   예: /attach departments://all")
                    else:
                        await cmd_attach(session, attached, parts[1].strip())
                    continue
                if line.startswith("/detach"):
                    parts = line.split(maxsplit=1)
                    uri = parts[1].strip() if len(parts) > 1 else ""
                    if attached.pop(uri, None) is not None:
                        print(f"첨부 해제: {uri}")
                    else:
                        print(f"첨부돼 있지 않음: {uri}")
                    continue

                # ── 질문 처리: 03 의 흐름에 첨부 자료 system 주입만 더한다. ──
                messages.append({"role": "user", "content": line})
                system = build_system(attached)
                log("**", f"(대화 {len(messages)} 메시지, 첨부 {len(attached)} 건)")
                answer = await run_turns(
                    session, client, tools_for_claude, messages, system
                )
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
