# -*- coding: utf-8 -*-
"""ops/apply_themes.py — одобренные владельцем темы → config/universe.yaml с провенансом (V2, Q-темы 09.11).

Субботняя карточка «утверди темы» (ops/bot_themes.py) пишет решения в journal/theme_decisions.jsonl; конфиг
бот НЕ трогает (сервер не правит закоммиченные файлы). Перенос делает облачная сессия:

  python3 ops/apply_themes.py            → что будет добавлено (ничего не пишет)
  python3 ops/apply_themes.py --apply    → вписать в config/universe.yaml (+ коммит делает сессия)

Правила: только «approved», последнее решение по ключу побеждает; тема, которая уже есть, не трогается;
у каждой новой темы — откуда она (событие, дата решения, ключ карточки). Файл после правки обязан
читаться YAML и сохранить все прежние темы — иначе отказ без записи.
"""
import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "config" / "universe.yaml"
DECISIONS = ROOT / "journal" / "theme_decisions.jsonl"


def _slug(key):
    s = re.sub(r"[^a-z0-9_]+", "_", str(key or "").lower()).strip("_")
    return ("owner_" + s)[:48] if s else None


def approved(decisions_path=None):
    last = {}
    p = pathlib.Path(decisions_path or DECISIONS)
    if not p.exists():
        return []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("type") == "theme_decision" and r.get("ключ"):
            last[r["ключ"]] = r
    return [r for r in last.values() if r.get("решение") == "approved"]


def _block(name, r):
    def q(x):
        return json.dumps(str(x), ensure_ascii=False)
    inst = [str(i) for i in (r.get("инструменты") or []) if str(i).strip()][:8]
    keys = [str(k) for k in (r.get("кластер_ключи") or []) if str(k).strip()][:5]
    L = [f"  {name}:",
         f"    related: [{', '.join(q(i) for i in inst)}]",
         f"    trends_keywords: [{', '.join(q(k) for k in keys)}]",
         f"    provenance: {q('одобрено владельцем ' + str(r.get('ts'))[:10] + ' в карточке тем, ключ ' + str(r.get('ключ')))}",
         f"    event: {q(r.get('событие') or '')}"]
    if r.get("узел"):
        L.append(f"    far_node: {q(r['узел'])}")
    return "\n".join(L) + "\n"


def plan(universe_text, rows):
    import yaml
    cur = (yaml.safe_load(universe_text) or {}).get("themes") or {}
    out = []
    for r in rows:
        name = _slug(r.get("ключ"))
        if not name or name in cur or not (r.get("инструменты") or r.get("кластер_ключи")):
            continue
        out.append((name, r))
    return out


def apply(universe_path=None, decisions_path=None, write=False):
    import yaml
    p = pathlib.Path(universe_path or UNIVERSE)
    text = p.read_text(encoding="utf-8")
    todo = plan(text, approved(decisions_path))
    if not todo:
        return {"добавлено": [], "причина": "одобренных новых тем нет"}
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(r"^themes:\s*(#.*)?$", ln)), None)
    if start is None:
        return {"добавлено": [], "ошибка": "в universe.yaml нет раздела themes"}
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
    block = "".join(_block(n, r) for n, r in todo)
    new = "".join(lines[:end]) + ("" if "".join(lines[:end]).endswith("\n") else "\n") + block + "".join(lines[end:])
    before = (yaml.safe_load(text) or {}).get("themes") or {}
    after = (yaml.safe_load(new) or {}).get("themes") or {}
    if not set(before) <= set(after) or not all(n in after for n, _r in todo):
        return {"добавлено": [], "ошибка": "после вставки YAML не сохранил прежние темы — отказ без записи"}
    if write:
        p.write_text(new, encoding="utf-8")
    return {"добавлено": [n for n, _r in todo], "записано": bool(write)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(apply(write=a.apply), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
