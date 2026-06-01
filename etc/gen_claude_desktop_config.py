"""
claude_desktop_config.json 에 붙여 넣을 mcpServers 설정 조각을 만든다.

실행:
  python etc/gen_claude_desktop_config.py

결과:
  etc/claude_desktop.json 파일에 아래 형식으로 저장된다:
    {
      "mcpServers": {
        "student-mcp": { command, args, cwd ... 현재 PC 경로로 채워짐 }
      }
    }

설계 의도:
  - 시스템 위치(%APPDATA%\\Claude\\...)를 직접 수정하지 않는다.
    사용자가 기존 설정을 확인하고 직접 병합해 보도록 하기 위해서다.
  - 생성된 설정 조각은 그 자체로 올바른 JSON 이다. 새 PC 에서는 통째로 사용할 수 있고,
    기존 config 가 있으면 'mcpServers' 키 내용만 병합하면 된다.
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
    # 이 파일이 <repo>/etc/ 안에 있다고 가정하고 프로젝트 루트를 계산한다.
    return Path(__file__).resolve().parent.parent


def venvPython(root: Path) -> Path:
    if platform.system() == "Windows":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def claudeConfigHint() -> Path:
    """OS 별 Claude Desktop config 경로를 안내용으로 반환한다."""
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

    # 파일이 없어도 생성을 막지는 않는다. 설정 조각을 만드는 것 자체는 안전하다.
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
                # Windows cp949 환경에서 Claude Desktop 이 실행한 Python 이
                # .py 파일을 잘못 디코딩하지 않도록 UTF-8 mode 를 강제한다.
                # 소스, 표준입출력, 파일 I/O 를 모두 UTF-8 기준으로 맞춘다.
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
