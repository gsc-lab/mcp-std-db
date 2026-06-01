"""
Stage 3 — 준비 단계: SDK 없이 직접 만든 JSON-RPC 클라이언트.

학습 목표:
  ClientSession SDK 가 자동으로 처리하던 송수신 과정을 직접 작성해 보고,
  stdio 위에서 어떤 JSON 문자열이 한 줄씩 오가는지 확인한다.
  이 단계를 거친 다음 01_single_turn.py 의 ClientSession 추상화로
  넘어가면 "SDK 가 이 작업을 대신 해주는구나" 를 쉽게 이해할 수 있다.

이 파일은 LLM(Anthropic)을 호출하지 않는다. MCP 통신 흐름 자체에만 집중한다.
요청 메시지 4종을 차례로 보낸다:
  1) initialize                  (request,      id=1)
  2) notifications/initialized   (notification, id 없음)
  3) tools/list                  (request,      id=2)
  4) tools/call                  (request,      id=3, name=department_stats)

stdio transport 규칙:
  - 한 줄에 한 JSON 메시지 (newline-delimited JSON)
  - 클라이언트 → 서버: stdin
  - 서버 → 클라이언트: stdout
  - 서버 자체 로그: stderr (= 현재 콘솔에 그대로 표시)

실행:
  python agent/00_raw_jsonrpc.py
"""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# Windows 콘솔에서 한글이 깨지지 않도록 UTF-8 출력으로 맞춘다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"

# MCP 프로토콜 버전. 강의 자료의 버전과 맞춰 둔다.
PROTOCOL_VERSION = "2025-11-25"


def venv_python() -> Path:
    """서버 실행에 사용할 프로젝트 가상환경의 Python 경로."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def send(proc: subprocess.Popen, message: dict) -> None:
    """클라이언트에서 서버로 JSON 한 줄을 보낸다.

    JSON-RPC stdio 에서는 메시지 하나가 JSON 한 줄이고, 끝에는 \\n 이 붙는다.
    """
    line = json.dumps(message, ensure_ascii=False)
    print(f">> {line}", flush=True)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def recv(proc: subprocess.Popen) -> dict:
    """서버가 stdout 으로 보낸 JSON 한 줄을 읽는다.

    NOTE: 서버가 응답하지 않으면 여기서 계속 기다린다. 학습용 예제라 타임아웃은
          생략했지만, 실제 서비스에서는 asyncio / select 등으로 제한 시간을 둔다.
    """
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("서버가 응답을 끊었습니다. stderr 를 확인해 보세요.")
    print(f"<< {line.rstrip()}", flush=True)
    return json.loads(line)


def main() -> int:
    # ─── 1) MCP 서버를 자식 프로세스로 실행 ───
    env = {**os.environ, "PYTHONUTF8": "1"}
    print(f"** MCP 서버 spawn: {SERVER_PATH}", flush=True)
    proc = subprocess.Popen(
        [str(venv_python()), str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,        # 서버 로그는 현재 콘솔에 그대로 표시
        text=True,
        encoding="utf-8",
        env=env,
        bufsize=1,                # 줄 단위 버퍼링: 한 줄이 생기면 바로 전달
    )

    try:
        # ─── 2) initialize 핸드셰이크 ───
        #   클라이언트가 사용할 프로토콜 버전과 기능을 서버에 알린다.
        #   서버는 자신의 정보와 지원 기능(capabilities)을 응답한다.
        send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "raw-jsonrpc-demo",
                    "version": "0.1",
                },
            },
        })
        init_resp = recv(proc)
        server_info = init_resp.get("result", {}).get("serverInfo", {})
        print(
            f"** 서버 이름: {server_info.get('name')}, "
            f"버전: {server_info.get('version')}",
            flush=True,
        )

        # ─── 3) initialized notification ───
        #   "이제 본격적으로 통신을 시작한다" 는 신호.
        #   notification 은 id 가 없으므로 서버 응답도 없다.
        send(proc, {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        # notification 은 응답이 없으므로 recv() 를 호출하지 않는다.

        # ─── 4) tools/list — 서버가 노출하는 도구 목록 ───
        send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        })
        list_resp = recv(proc)
        tools = list_resp.get("result", {}).get("tools", [])
        print(
            f"** 도구 {len(tools)} 개 발견: "
            f"{[t['name'] for t in tools]}",
            flush=True,
        )

        # ─── 5) tools/call — 인자 없는 department_stats 호출 ───
        #   처음에는 인자가 없는 도구가 가장 이해하기 쉽다.
        #   search_students / top_students 는 인자를 직접 채워보는 후속 연습에 좋다.
        send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "department_stats",
                "arguments": {},
            },
        })
        call_resp = recv(proc)
        # result.content 는 ContentBlock 리스트이며, 보통 text 블록이 들어 있다.
        content = call_resp.get("result", {}).get("content", [])
        text_blocks = [b for b in content if b.get("type") == "text"]

        print("\n=== department_stats 결과 (text 블록) ===")
        for b in text_blocks:
            print(b["text"])

    finally:
        # ─── 6) 정리 ───
        #   stdin 을 닫으면 서버가 EOF 를 감지하고 종료한다.
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
