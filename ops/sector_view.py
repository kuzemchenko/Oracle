# -*- coding: utf-8 -*-
"""ops/sector_view.py — «КАРТА СЕКТОРОВ»: что сейчас происходит с сектором бумаги (Q-вердикт-комитет, п.7 листа).

Владелец 22.11: «оцени не только новости, но и общие возможности — этот сектор будет расти или, возможно,
корректироваться». Код не предсказывает: он измеряет по ценам биржевого фонда сектора долгий тренд, ход за
3 месяца против широкого рынка и расстояние до годового максимума, и по правилам mathlib/sector_rules ставит
статус: попутный / нейтральный / корректируется / перегрет. LLM нет (Инв#6). Правила и соответствие
«сектор → фонд» — knowledge/idea_committee.yaml (раздел «сектор»).

Цены — боевая база (storage/oracle.db, только чтение), нет в базе — EODHD. Сектор бумаги — ручной список листа,
затем кэш, затем EODHD fundamentals (General). Нет данных → честное «не измерено».

  python3 ops/sector_view.py show                 → карта секторов (ничего не пишет)
  python3 ops/sector_view.py ticker S.US [--direction вверх|вниз]
  python3 ops/sector_view.py tick                 → датированный снимок reports/sectors/<дата>.json (+ latest.json)
"""
import argparse
import datetime
import json
import os
import pathlib
import sqlite3
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from mathlib import sector_rules as SR      # noqa: E402

RULES = ROOT / "knowledge" / "idea_committee.yaml"
DB = ROOT / "storage" / "oracle.db"
OUT = ROOT / "reports" / "sectors"
CACHE = OUT / "ticker_sector.json"
BASE = "https://eodhd.com/api/"
HISTORY_DAYS = 420          # календарных: с запасом на 252 торговых дня


def load_rules(path=None):
    d = yaml.safe_load(pathlib.Path(path or RULES).read_text(encoding="utf-8")) or {}
    return d.get("сектор") or {}


def _sym(t):
    t = str(t or "").strip().upper()
    return t if "." in t else (t + ".US" if t else t)


# ── источники (заменяемые функции: тесты герметичны, сбой одного не роняет вердикт) ─────────────────
def _db_closes(sym, today, db_path=None):
    p = pathlib.Path(db_path or DB)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
        rows = con.execute(
            "select date, coalesce(adjusted_close, close) from quotes where symbol=? and date>=? and date<=? "
            "order by date", (sym, (today - datetime.timedelta(days=HISTORY_DAYS)).isoformat(), today.isoformat())
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    rows = [(d, float(c)) for d, c in rows if c is not None]
    return rows or None


def _api(path, params):
    import requests
    key = os.environ.get("EODHD_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(BASE + path, params=dict(params, fmt="json", api_token=key), timeout=60)
        return r.json() if r.status_code == 200 else None
    except (OSError, ValueError):
        return None


def _api_closes(sym, today):
    data = _api(f"eod/{sym}", {"from": (today - datetime.timedelta(days=HISTORY_DAYS)).isoformat(),
                               "to": today.isoformat()})
    if not isinstance(data, list):
        return None
    return [(str(r["date"])[:10], float(r.get("adjusted_close") or r["close"])) for r in data
            if (r.get("adjusted_close") or r.get("close")) is not None] or None


GAP = 0.30                  # ход фонда за один день больше 30% — не рынок, а дробление акций без поправки в базе


def has_gap(closes, thr=GAP):
    v = [c for _, c in closes or []]
    return any(v[i - 1] and abs(v[i] / v[i - 1] - 1) >= thr for i in range(1, len(v)))


def fetch_closes(sym, today):
    """База → при разрыве ряда (22.11: ITA после дробления 05.11 в базе «−68% за день», прежние цены не
    пересчитаны) — свежий ряд EODHD с поправкой; разрыв и там → None («не измерено», а не ложное «−70%»)."""
    rows = _db_closes(sym, today)
    if not rows or has_gap(rows):
        rows = _api_closes(sym, today)
    return None if (not rows or has_gap(rows)) else rows


def fetch_general(sym, today=None):
    """EODHD fundamentals General → {'sector', 'industry'} или None."""
    g = _api(f"fundamentals/{sym}", {"filter": "General"})
    if not isinstance(g, dict):
        return None
    return {"sector": g.get("GicSector") or g.get("Sector"),
            "industry": " | ".join(str(x) for x in (g.get("GicSubIndustry"), g.get("Industry")) if x)}


FETCH = {"closes": fetch_closes, "general": fetch_general}


# ── сектор бумаги ───────────────────────────────────────────────────────────────────────────────────
def _cache_load(path=None):
    try:
        return json.loads(pathlib.Path(path or CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cache_save(c, path=None):
    p = pathlib.Path(path or CACHE)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(c, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def fund_name(fund, rules):
    names = dict(rules.get("имена_фондов") or {})
    for v in (rules.get("фонды") or {}).values():
        names.setdefault(v["фонд"], v.get("имя"))
    for o in rules.get("отрасли") or []:
        names.setdefault(o["фонд"], o.get("имя"))
    return names.get(fund) or fund.split(".")[0]


def fund_for(ticker, rules=None, fetch=None, cache_path=None, use_cache=True):
    """Бумага → {'фонд', 'имя', 'откуда'} или {'причина'} (сектор не определён — честно)."""
    rules = rules if rules is not None else load_rules()
    sym = _sym(ticker)
    for fund, syms in (rules.get("бумаги") or {}).items():
        if sym in syms or sym == fund:
            return {"фонд": fund, "имя": fund_name(fund, rules), "откуда": "список листа комитета"}
    cache = _cache_load(cache_path) if use_cache else {}
    g = cache.get(sym)
    if not g:
        try:
            g = (fetch or FETCH)["general"](sym)
        except Exception:                                   # noqa: BLE001 — нет сектора = «не измерено»
            g = None
        if g and (g.get("sector") or g.get("industry")) and use_cache:     # пустой ответ не кэшируем
            cache[sym] = g
            _cache_save(cache, cache_path)
    if not g or not (g.get("sector") or g.get("industry")):
        return {"причина": "сектор бумаги не определён (нет данных EODHD)"}
    ind = str(g.get("industry") or "").lower()
    for o in rules.get("отрасли") or []:
        if any(w in ind for w in o.get("слова") or []) and not any(w in ind for w in o.get("кроме") or []):
            return {"фонд": o["фонд"], "имя": o.get("имя") or fund_name(o["фонд"], rules),
                    "откуда": f"отрасль EODHD: {g.get('industry')}"}
    f = (rules.get("фонды") or {}).get(str(g.get("sector") or ""))
    if not f:
        return {"причина": f"для сектора «{g.get('sector')}» фонд не задан в листе комитета"}
    return {"фонд": f["фонд"], "имя": f.get("имя"), "откуда": f"сектор EODHD: {g.get('sector')}"}


# ── состояние ───────────────────────────────────────────────────────────────────────────────────────
def fund_state(fund, today=None, rules=None, fetch=None, _market=None):
    rules = rules if rules is not None else load_rules()
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    f = fetch or FETCH
    try:
        closes = f["closes"](fund, today)
        market = _market if _market is not None else f["closes"](_sym(rules.get("рынок") or "SPY.US"), today)
    except Exception:                                       # noqa: BLE001
        closes, market = None, None
    ms = SR.measures(closes, market, rules)
    st, why = SR.status(ms, rules)
    return {"фонд": fund, "имя": fund_name(fund, rules), "статус": st, "почему": why, "меры": ms}


def for_ticker(ticker, direction="вверх", today=None, rules=None, fetch=None, cache_path=None, use_cache=True):
    """Бумага + направление идеи → состояние её сектора и «против идеи / нет».
    → {'фонд','имя','статус','почему','меры','против': True|False|None, 'словами'}"""
    rules = rules if rules is not None else load_rules()
    f = fund_for(ticker, rules, fetch, cache_path, use_cache)
    if "фонд" not in f:
        return {"статус": None, "против": None, "словами": f["причина"]}
    st = fund_state(f["фонд"], today, rules, fetch)
    ag = SR.against(st["статус"], direction, rules)
    if st["статус"] is None:
        words = f"сектор «{f['имя']}»: состояние не измерено ({'; '.join(st['почему'])})"
    else:
        words = (f"сектор «{f['имя']}» (фонд {f['фонд'].split('.')[0]}) — {st['статус']}: "
                 + ", ".join(st["почему"]))
    return {**st, "откуда": f.get("откуда"), "против": ag, "словами": words}


def sector_map(today=None, rules=None, fetch=None):
    rules = rules if rules is not None else load_rules()
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    f = fetch or FETCH
    try:
        market = f["closes"](_sym(rules.get("рынок") or "SPY.US"), today)
    except Exception:                                       # noqa: BLE001
        market = None
    rows = [fund_state(x, today, rules, fetch, _market=market) for x in rules.get("карта") or []]
    return {"дата": today.isoformat(), "секторы": rows}


ORDER = {SR.TAILWIND: 0, SR.NEUTRAL: 1, SR.HOT: 2, SR.CORRECTING: 3, None: 4}


def render(m):
    """Карта секторов словами (пишет код). Это измерение «что сейчас», а не прогноз."""
    L = [f"Карта секторов на {m['дата']} — что происходит сейчас (по ценам фондов секторов; это измерение, не прогноз)."]
    rows = sorted(m["секторы"], key=lambda r: (ORDER.get(r["статус"], 4), -((r.get("меры") or {}).get("за_3_месяца") or 0)))
    cur = object()
    for r in rows:
        if r["статус"] != cur:
            cur = r["статус"]
            L.append("")
            L.append((cur or "не измерено").capitalize() + ":")
        L.append(f"• {r['имя']} ({r['фонд'].split('.')[0]}): " + ", ".join(r["почему"]))
    L.append("")
    L.append("Как читать: «попутный» — выше своей долгой средней и не хуже рынка; «корректируется» — ниже средней и "
             "падает три месяца; «перегрет» — далеко выше средней у самого годового максимума.")
    return "\n".join(L)


BRIEF_MAX_AGE_DAYS = 4


def market_brief(today=None, out_dir=None):
    """Две-три строки для сводки агента: что с рынком и какие секторы попутные / корректируются / перегреты — из
    последнего снимка карты (не старше BRIEF_MAX_AGE_DAYS дней). Нет свежего снимка → None (агент не гадает)."""
    try:
        m = json.loads((pathlib.Path(out_dir or OUT) / "latest.json").read_text(encoding="utf-8"))
        today = today or datetime.datetime.now(datetime.timezone.utc).date()
        if (today - datetime.date.fromisoformat(str(m["дата"]))).days > BRIEF_MAX_AGE_DAYS:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    rows = [r for r in m.get("секторы") or [] if r.get("статус")]
    mkt = next(((r.get("меры") or {}).get("рынок_за_3_месяца") for r in rows
                if (r.get("меры") or {}).get("рынок_за_3_месяца") is not None), None)

    def names(st):
        return ", ".join(f"{r['имя']} ({SR._pct((r.get('меры') or {}).get('за_3_месяца') or 0)} за 3 мес.)"
                         for r in rows if r["статус"] == st)
    parts = [f"РЫНОК И СЕКТОРЫ на {m['дата']} (код, по ценам фондов секторов; измерение, не прогноз): "
             + (f"широкий рынок за 3 месяца {SR._pct(mkt)}." if mkt is not None else "ход широкого рынка не измерен.")]
    for st, label in ((SR.CORRECTING, "Корректируются"), (SR.HOT, "Перегреты"), (SR.TAILWIND, "Попутные"),
                      (SR.NEUTRAL, "Нейтральные")):
        if names(st):
            parts.append(f"{label}: {names(st)}.")
    n_down = sum(1 for r in rows if r["статус"] == SR.CORRECTING)
    if rows and n_down * 2 >= len(rows):
        parts.append(f"Корректируется {n_down} из {len(rows)} секторов: идеи только на покупку идут против рынка — "
                     "рассмотри, кто ПРОИГРЫВАЕТ от события (идея на падение), и «не покупать» как честный ответ.")
    return " ".join(parts)


def tick(today=None, out_dir=None, rules=None, fetch=None):
    m = sector_map(today, rules, fetch)
    d = pathlib.Path(out_dir or OUT)
    d.mkdir(parents=True, exist_ok=True)
    for name in (f"{m['дата']}.json", "latest.json"):
        (d / name).write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description="Карта секторов: состояние сектора по ценам его фонда")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "tick"):
        p = sub.add_parser(name)
        p.add_argument("--today", default=None)
    p = sub.add_parser("ticker")
    p.add_argument("ticker")
    p.add_argument("--direction", default="вверх", choices=("вверх", "вниз"))
    p.add_argument("--today", default=None)
    a = ap.parse_args(argv)
    today = datetime.date.fromisoformat(a.today) if a.today else None
    if a.cmd == "ticker":
        print(json.dumps(for_ticker(a.ticker, a.direction, today), ensure_ascii=False, indent=1))
    elif a.cmd == "show":
        print(render(sector_map(today)))
    else:
        print(render(tick(today)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
