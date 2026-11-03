# -*- coding: utf-8 -*-
"""ops/price_snapshot.py — снимок серверных закрытий EODHD для облачного охотника (аудит 03.11).

ПОЧЕМУ: облачная охота 03.11 (второй круг) вернулась с пустыми руками — общий лимит веб-поиска
кончился, а сайты котировок из облака закрыты; без цены закрытия в день повода шлагбаум не считает
ход, и сильные кандидаты (Lennar, MSG Rangers) отпали. При этом на сервере лежат закрытия EODHD.

КАК: бот каждый вечер (флаг price_snapshot_auto, после 21:30 UTC) пишет reports/prices/closes_latest.json
— последние ~130 торговых дней закрытий по всем бумагам серверной базы со свежими данными; вечерний
чекпоинт (22:00) увозит его в GitHub, утренний охотник (03:17) берёт цены отсюда:
  python3 ops/price_snapshot.py get MP.US 2026-09-23   → закрытие на дату повода (или ближайшее раньше),
                                                          последнее закрытие, дневная σ по 60 закрытиям.
Колонка — сырой close (та же, что у серверного шлагбаума и печати: допуск 3% сверяет одно и то же).
Только чтение базы; журналы не трогает. Сервер всё равно пересчитает цифры по своей базе в момент доставки.
"""
import argparse
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB = ROOT / "storage" / "oracle.db"
OUT = ROOT / "reports" / "prices" / "closes_latest.json"
DAYS = 130            # ≥60 для σ + запас на дату повода до ~3 месяцев назад
FRESH_DAYS = 10       # бумаги без закрытий за последние 10 дней не берём (мертвые/делистинг)


def build(db_path=None, days=DAYS, fresh_days=FRESH_DAYS, today=None):
    db = pathlib.Path(db_path or DB)
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    out = {"as_of": None, "source": "EODHD close из серверной базы storage/oracle.db (сырой close)",
           "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "symbols": {}}
    if not db.exists():
        return out
    con = sqlite3.connect(str(db), timeout=30)
    try:
        cut = (today - datetime.timedelta(days=fresh_days)).isoformat()
        syms = [r[0] for r in con.execute(
            "SELECT symbol FROM quotes WHERE close IS NOT NULL GROUP BY symbol HAVING MAX(date) >= ?", (cut,))]
        last = None
        for s in sorted(syms):
            rows = con.execute("SELECT date, close FROM quotes WHERE symbol=? AND close IS NOT NULL "
                               "ORDER BY date DESC LIMIT ?", (s, days)).fetchall()
            if not rows:
                continue
            out["symbols"][s] = [[str(d)[:10], round(float(c), 4)] for d, c in reversed(rows)]
            last = max(last or "", str(rows[0][0])[:10])
        out["as_of"] = last
    finally:
        con.close()
    return out


def write(path=None, **kw):
    snap = build(**kw)
    p = pathlib.Path(path or OUT)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)
    return snap


def lookup(symbol, on_date=None, path=None):
    """→ {symbol, as_of, last_date, last_close, on_or_before: {date, close}|None, sigma_daily, source}
    или {"ошибка": …}. Дата повода позже последнего закрытия или раньше истории — честно None."""
    from mathlib import base_odds as BO
    p = pathlib.Path(path or OUT)
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ошибка": f"снимка цен нет ({p}) — цены только из первоисточника"}
    sym = str(symbol).upper()
    if not sym.endswith(".US") and "." not in sym:
        sym += ".US"
    rows = (snap.get("symbols") or {}).get(sym)
    if not rows:
        return {"ошибка": f"бумаги {sym} в снимке нет — цену ищи в первоисточнике, сервер сверит сам"}
    res = {"symbol": sym, "as_of": snap.get("as_of"), "source": snap.get("source"),
           "last_date": rows[-1][0], "last_close": rows[-1][1], "on_or_before": None,
           "sigma_daily": BO.daily_sigma([c for _d, c in rows[-61:]])}
    if on_date:
        prior = [r for r in rows if r[0] <= str(on_date)[:10]]
        if prior and str(on_date)[:10] >= rows[0][0]:
            res["on_or_before"] = {"date": prior[-1][0], "close": prior[-1][1]}
        # ре-гейт 03.11: закрытие ПРЕДЫДУЩЕГО дня — не цена в день повода (выходной/праздник — да, но если
        # дата повода позже снимка, это просто «ещё нет данных»): охотник берёт trigger_close только при точном
        res["точно_на_дату"] = bool(res["on_or_before"] and res["on_or_before"]["date"] == str(on_date)[:10])
        if str(on_date)[:10] > rows[-1][0]:
            res["on_or_before"] = None
            res["точно_на_дату"] = False
            res["примечание"] = "дата повода позже снимка — закрытия ещё нет; не подставляй прошлое"
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="снимок серверных закрытий для охотника")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("write", help="сервер: записать reports/prices/closes_latest.json")
    g = sub.add_parser("get", help="облако: цена бумаги из снимка")
    g.add_argument("symbol")
    g.add_argument("date", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "write":
        snap = write()
        print(f"[price_snapshot] {len(snap['symbols'])} бумаг, последнее закрытие {snap['as_of']}")
        return 0
    r = lookup(a.symbol, a.date)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if "ошибка" not in r else 3


if __name__ == "__main__":
    sys.exit(main())
