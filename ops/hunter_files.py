# -*- coding: utf-8 -*-
"""ops/hunter_files.py — файлы охоты пишет КОД, а не модель (перепроверка №2, 03.11).

Прежде воркфлоу hunt-idea.js передавал карточки теней и лидера модели-автору через JSON, обрезанный
по .slice(0, 60000), и просил переписать «как есть»: при ≥4 отвергнутых тени терялись или
обрывались посреди объекта, а модель их «достраивала» (П8). Теперь в промпт идут только ПУТИ к
карточкам, прошедшим шлагбаум (gate_file), а эти команды копируют их байт-в-байт по смыслу:

  python3 ops/hunter_files.py card <gate_file> <stem>.card.json     — карточка лидера
  python3 ops/hunter_files.py shadow <out.shadow.json> [gate_file…]  — список теней (FATAL с §9)

Тень берётся, только если у неё полная §9-спека (её запечатает сервер тенью). Код выхода 0/1.
"""
import json
import pathlib
import sys

SPEC_KEYS = ("asset", "direction", "threshold", "resolve_by", "probability", "last_close",
             "last_close_date")


def _load(path):
    c = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(c, dict):
        raise ValueError(f"{path}: карточка не объект")
    return c


def full_spec(card):
    s = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    return all(s.get(k) not in (None, "") for k in SPEC_KEYS)


def write_card(src, dst):
    c = _load(src)
    c.pop("gate_file", None)
    pathlib.Path(dst).write_text(json.dumps(c, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return c


def write_shadows(out, srcs):
    rows, skipped = [], []
    for p in srcs:
        try:
            c = _load(p)
        except (OSError, ValueError) as e:
            skipped.append(f"{p}: {e}")
            continue
        if not full_spec(c):
            skipped.append(f"{p}: нет полной §9-спеки")
            continue
        c.pop("gate_file", None)
        rows.append(c)
    pathlib.Path(out).write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return rows, skipped


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) >= 3 and a[0] == "card":
        c = write_card(a[1], a[2])
        print(json.dumps({"ok": True, "ticker": c.get("ticker"), "dst": a[2]}, ensure_ascii=False))
        return 0
    if len(a) >= 2 and a[0] == "shadow":
        rows, skipped = write_shadows(a[1], a[2:])
        print(json.dumps({"ok": True, "shadows": len(rows), "skipped": skipped}, ensure_ascii=False))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
