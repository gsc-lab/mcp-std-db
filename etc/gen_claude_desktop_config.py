"""
claude_desktop_config.json 에 머지할 mcpServers 스니펫을 생성.

실행:
  python etc/gen_claude_desktop_config.py

결과:
  etc/claude_desktop.json 에 아래 형식으로 작성됨:
    {
      "mcpServers": {
        "student-mcp": { command, args, cwd ... 현재 PC 경로로 채워짐 }
      }
    }

설계 의도:
  - 시스템 위치(%APPDATA%\\Claude\\...)를 직접 쓰지 않음 — 사용자가 기존 파일 상태를
    스스로 확인하고 수동으로 머지하도록 유도. 학습 단계에선 "내 손으로 붙여넣어 본다"가
    실수보다 큰 가치.
  - 생성된 스니펫은 그 자체로 valid JSON — 새 PC면 통째로 사용해도 되고, 기존 config가
    있으면 'mcpServers' 키 내용만 머지.
"""
import json
import os
import platform
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SERVER_NAME = "student-mcp"
OUTPUT = Path(__file__).resolve().parent / "claude_desktop.json"


def projectRoot() -> Path:
    # 이 파일은 <repo>/etc/ 안에 있다고 가정.
    return Path(__file__).resolve().parent.parent


def venvPython(root: Path) -> Path:
    if platform.system() == "Windows":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def claudeConfigHint() -> Path:
    """OS 별 Claude Desktop config 경로 — 안내용으로만 사용."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def main() -> int:
    root = projectRoot()
    py = venvPython(root)
    main_py = root / "server" / "main.py"

    # 막지는 않음 — 경로만 알려주고 진행. (스니펫 생성 자체는 무해)
    if not py.exists():
        print(f"[!] venv 파이썬 없음: {py}", file=sys.stderr)
        print(f"    먼저 만들어야 Claude Desktop이 서버를 spawn 가능: python -m venv .venv", file=sys.stderr)
    if not main_py.exists():
        print(f"[!] 서버 스크립트 없음: {main_py}", file=sys.stderr)

    snippet = {
        "mcpServers": {
            SERVER_NAME: {
                "command": str(py),
                "args": [str(main_py)],
                "cwd": str(root),
                # Windows cp949 환경에서 Claude Desktop이 spawn한 Python이
                # 소스 파일(.py)을 cp949 로 잘못 디코딩하는 케이스 방지.
                # PEP 540 (UTF-8 mode) 강제: 소스/표준입출력/파일 I/O 모두 UTF-8.
                "env": {"PYTHONUTF8": "1"},
            }
        }
    }
    OUTPUT.write_text(
        json.dumps(snippet, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"[OK] 생성: {OUTPUT}")
    print()
    print(json.dumps(snippet, indent=2, ensure_ascii=False))
    print()
    print("다음 단계:")
    print(f"  1) 위 'mcpServers' 항목을 아래 파일에 머지 (없으면 새로 만들기):")
    print(f"     {claudeConfigHint()}")
    print(f"  2) Claude Desktop을 시스템 트레이에서 완전 종료 후 재실행")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
