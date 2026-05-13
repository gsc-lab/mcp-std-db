"""
Stage 3 — pre-step: SDK 없이 손으로 짠 raw JSON-RPC 클라이언트.

학습 목표:
  ClientSession SDK 가 자동으로 해주던 송수신을 우리 손으로 짜서,
  stdio 위로 어떤 JSON 문자열이 한 줄씩 오가는지 직접 본다.
  이 단계를 거친 다음 01_single_turn.py 의 ClientSession 추상화로
  넘어가면 "아, SDK 가 이걸 대신 해주는 거구나" 가 한눈에 보인다.

이 파일은 LLM(Anthropic) 호출은 하지 않는다. MCP wire 그 자체에 집중.
요청 메시지 4종을 차례로 보낸다:
  1) initialize                  (request,      id=1)
  2) notifications/initialized   (notification, id 없음)
  3) tools/list                  (request,      id=2)
  4) tools/call                  (request,      id=3, name=department_stats)

stdio transport 약속:
  - 한 줄에 한 JSON 메시지 (newline-delimited JSON)
  - 클라이언트 → 서버: stdin
  - 서버 → 클라이언트: stdout
  - 서버 자체 로그: stderr (= 우리 콘솔로 그대로 흘림)

실행:
  python agent/00_raw_jsonrpc.py
"""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# Windows cp949 → 한글 출력 깨짐 방지.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "server" / "main.py"

# MCP 최신 사양 버전. Ch.3 slide 32 와 동기화.
PROTOCOL_VERSION = "2025-11-25"


def venv_python() -> Path:
    """01_single_turn.py 와 동일 규칙 — 서버는 venv 인터프리터로 실행."""
    if platform.system() == "Windows":
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return REPO_ROOT / ".venv" / "bin" / "python"


def send(proc: subprocess.Popen, message: dict) -> None:
    """우리(클라이언트) → 서버. JSON 한 줄을 stdin 으로 송신.

    JSON-RPC stdio 규약: 메시지 = 한 줄 JSON. 끝에 \\n.
    """
    line = json.dumps(message, ensure_ascii=False)
    print(f">> {line}", flush=True)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def recv(proc: subprocess.Popen) -> dict:
    """서버 → 우리. stdout 에서 JSON 한 줄을 수신.

    NOTE: 서버가 응답을 안 주면 무한 hang. 학습용이라 타임아웃은 두지 않음.
          실전에선 asyncio / select 로 deadline 을 걸어야 한다.
    """
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("서버가 응답을 끊었습니다. stderr 를 확인해 보세요.")
    print(f"<< {line.rstrip()}", flush=True)
    return json.loads(line)


def main() -> int:
    # ─── 1) MCP 서버를 subprocess 로 spawn ───
    env = {**os.environ, "PYTHONUTF8": "1"}
    print(f"** MCP 서버 spawn: {SERVER_PATH}", flush=True)
    proc = subprocess.Popen(
        [str(venv_python()), str(SERVER_PATH)],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,        # 서버 로그는 우리 콘솔로 그대로 흘림
        text=True,
        encoding="utf-8",
        env=env,
        bufsize=1,                # line-buffered — 줄 단위 즉시 flush
    )

    try:
        # ─── 2) initialize 핸드셰이크 ───
        #   클라이언트가 자기 프로토콜 버전 / 기능을 알림.
        #   서버는 자기 정보 + 지원 capabilities 를 돌려준다.
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
        #   "이제 본격 통신 시작" 신호. notification = id 없음 = 응답 없음.
        send(proc, {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        # ↑ recv 하지 않음. notification 은 응답이 없다.

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
        #   학생이 처음 보기엔 인자 없는 도구가 가장 단순. search_students /
        #   top_students 는 인자 채우기를 직접 해보면 좋은 후속 연습.
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
        # result.content 는 ContentBlock 리스트. 보통 [{"type":"text","text":"..."}].
        content = call_resp.get("result", {}).get("content", [])
        text_blocks = [b for b in content if b.get("type") == "text"]

        print("\n=== department_stats 결과 (text 블록) ===")
        for b in text_blocks:
            print(b["text"])

    finally:
        # ─── 6) 정리 ───
        #   stdin 을 닫으면 서버가 EOF 를 보고 자체 종료한다.
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
