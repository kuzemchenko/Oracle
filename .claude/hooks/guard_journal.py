#!/usr/bin/env python3
"""PreToolUse-хук: запрещает изменять запечатанные журналы (П16, инвариант 3 CLAUDE.md).
Выход с кодом 2 = блокировка действия, сообщение из stderr возвращается Claude."""
import json, sys

PROTECTED = [
    "journal/predictions.jsonl",
    "journal/outcomes.jsonl",       # ревью 2026-07-04: вторая половина Brier/§11 — та же защита
    "journal/holdout_access.log",
    "journal/decisions_user.jsonl",
]
# Подстрочное совпадение прикрывает и сайдкары <журнал>.anchor.json / .lock (внешний якорь цепочки).

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    text = " ".join(str(ti.get(k, "")) for k in ("file_path", "path", "command", "new_str", "old_str"))
    # Запись через bash тоже ловим (>>, >, sed -i, tee)
    for p in PROTECTED:
        if p in text:
            if tool == "Bash":
                cmd = str(ti.get("command", ""))
                # чтение разрешено, запись — нет
                if any(w in cmd for w in [">", "tee ", "sed -i", "truncate", "rm ", "mv "]):
                    print(f"БЛОКИРОВКА: {p} — запечатанный журнал, запись запрещена (П16). "
                          f"Добавление прогнозов — только через mathlib.seal() из оркестратора.", file=sys.stderr)
                    sys.exit(2)
            else:  # Edit / Write / MultiEdit / NotebookEdit
                print(f"БЛОКИРОВКА: {p} — запечатанный журнал, редактирование запрещено (П16).", file=sys.stderr)
                sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()
