#!/usr/bin/env python3
"""PostToolUse-хук: после правки файлов в mathlib/ гоняет pytest.
Красные тесты => код 2, Claude обязан чинить (инвариант 6 CLAUDE.md)."""
import json, subprocess, sys, os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _python():
    """Интерпретатор с установленным pytest: сначала .venv проекта (там numpy/scipy/pytest),
    затем тот, что запустил хук, затем системный. Системный python3 pytest НЕ имеет —
    поэтому .venv обязателен (см. README_КОМАНДЫ.md)."""
    venv = os.path.join(ROOT, ".venv", "bin", "python")
    if os.path.exists(venv):
        return venv
    return sys.executable or "python3"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    ti = data.get("tool_input", {}) or {}
    fp = str(ti.get("file_path", "") or ti.get("path", ""))
    if "mathlib/" not in fp:
        sys.exit(0)
    if not os.path.isdir(os.path.join(ROOT, "mathlib", "tests")):
        sys.exit(0)
    r = subprocess.run([_python(), "-m", "pytest", "mathlib/tests", "-q", "--no-header"],
                       capture_output=True, text=True, timeout=300, cwd=ROOT)
    if r.returncode != 0:
        print("ТЕСТЫ MATHLIB КРАСНЫЕ после твоей правки — почини до продолжения:\n"
              + (r.stdout or "")[-2000:], file=sys.stderr)
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()
