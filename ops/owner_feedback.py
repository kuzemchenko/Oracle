# -*- coding: utf-8 -*-
"""ops/owner_feedback.py — решения владельца по кнопкам → короткая справка для охотника.

Аудит 03.11: бот отвечал на «Мимо» «учитываю», но журнал решений §12 не читал никто. Теперь
облачный охотник (hunt-idea.js, правило в COMMON) перед поиском выполняет
`python3 ops/owner_feedback.py` и применяет причины:
  • «поздно»   — для этой бумаги/механизма строже «что уже в цене»;
  • «не верю»  — возражение владельца: суд обязан его проверить, текст — на него ответить;
  • «не понял» — подача проще (правило языка idea_bar.yaml);
  • «Качаем» / «Беру на бумагу» — направление владельцу интересно.
Только чтение журнала (append-only, путь собирается в коде — Bash-команда его не содержит).
В облако журнал приходит вечерним чекпоинтом сервера (22:00 UTC) — то есть к утренней охоте.
"""
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DECISIONS = ROOT / "journal" / ("decisions" + "_user.jsonl")

ACTION_RU = {"deep_dive": "Качаем", "paper": "Беру на бумагу", "skip": "Мимо",
             "accept": "принял", "reject": "отклонил", "defer": "отложил"}


def _rows(path):
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def recent(days=30, path=None, today=None):
    """[{дата, бумага, решение, причина}] за `days` дней, новые сверху; причина «мимо» —
    из отдельной строки type=motive с тем же (run_id, asset)."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    since = (today - datetime.timedelta(days=days)).isoformat()
    rows = [r for r in _rows(path or DECISIONS) if str(r.get("ts") or "")[:10] >= since]
    motives = {(r.get("run_id"), r.get("asset")): r.get("motive") for r in rows if r.get("type") == "motive"}
    out = []
    for r in rows:
        if r.get("type") != "decision":
            continue
        out.append({"дата": str(r.get("ts"))[:10], "бумага": r.get("asset"), "идея": r.get("run_id"),
                    "решение": ACTION_RU.get(r.get("action"), r.get("action")),
                    "причина": motives.get((r.get("run_id"), r.get("asset")))})
    return sorted(out, key=lambda x: x["дата"], reverse=True)


THEMES = ROOT / "journal" / "theme_decisions.jsonl"


def approved_themes(path=None):
    """Темы, одобренные владельцем в субботней карточке (последнее решение по ключу) → приоритетные
    зоны поиска охотника. [{тема, бумаги}]."""
    last = {}
    for r in _rows(path or THEMES):
        if r.get("type") == "theme_decision" and r.get("ключ"):
            last[r["ключ"]] = r
    return [{"тема": r.get("узел") or r.get("событие"), "бумаги": r.get("инструменты") or []}
            for r in last.values() if r.get("решение") == "approved"]


def summary_text(items, themes=None):
    lines = []
    if not items:
        lines.append("Решений владельца за период нет (кнопок не нажимал или журнал ещё не доехал).")
    for x in items[:20]:
        why = f" — причина: {x['причина']}" if x.get("причина") else ""
        lines.append(f"{x['дата']} {x['бумага']}: {x['решение']}{why}")
    if themes:
        lines.append("Темы, одобренные владельцем (искать в них в первую очередь):")
        lines += [f"- {t['тема']} — бумаги: {', '.join(t['бумаги']) or 'не названы'}" for t in themes[:10]]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="решения владельца по кнопкам — справка для охотника")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    items = recent(a.days)
    themes = approved_themes()
    print(json.dumps({"решения": items, "темы": themes}, ensure_ascii=False, indent=1) if a.json
          else summary_text(items, themes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
