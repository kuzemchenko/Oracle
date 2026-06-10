#!/usr/bin/env python3
"""PostToolUse-хук: после правки файлов в mathlib/ гоняет pytest.
Красные тесты => код 2, Claude обязан чинить (инвариант 6 CLAUDE.md)."""
import json, subprocess, sys, os

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    ti = data.get("tool_input", {}) or {}
    fp = str(ti.get("file_path", "") or ti.get("path", ""))
    if "mathlib/" not in fp:
        sys.exit(0)
    if not os.path.isdir("mathlib/tests"):
        sys.exit(0)
    r = subprocess.run(["python", "-m", "pytest", "mathlib/tests", "-q", "--no-header"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print("ТЕСТЫ MATHLIB КРАСНЫЕ после твоей правки — почини до продолжения:\n"
              + (r.stdout or "")[-2000:], file=sys.stderr)
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()
