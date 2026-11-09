# -*- coding: utf-8 -*-
"""ops/options_view.py — что опционы закладывают к дате идеи (очередь Q-в-цене, 09.11). Только чтение EODHD.

  python3 ops/options_view.py FSLR 2027-01-08 [--threshold 230] [--direction above|below] [--spot 201.14]

Цена акции — снимок сервера (ops/price_snapshot) или --spot; опционы — EODHD Unicorn Bay. Ключ: в облаке
его подставляет сеть окружения, на сервере — EODHD_API_KEY из .env (в вывод и журналы не попадает).
Расчёт — mathlib/options_implied (детерминированно)."""
import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
URL = "https://eodhd.com/api/mp/unicornbay/options/eod"
MAX_PAGES = 8


def fetch(ticker, exp_from, exp_to, get=None, trade_from=None):
    """Контракты по бумаге с экспирацией в окне — ТОЛЬКО последний торговый день (без фильтра по дате торгов
    EODHD отдаёт историю каждого контракта, и 1000 строк уходит на одну ближнюю экспирацию). → (rows, ошибка)."""
    if get is None:
        import requests

        def get(url, params):
            r = requests.get(url, params=params, timeout=60)
            return r.status_code, (r.json() if r.status_code == 200 else r.text[:200])
    sym = str(ticker).upper().split(".")[0]
    params = {"filter[underlying_symbol]": sym, "filter[exp_date_from]": exp_from, "filter[exp_date_to]": exp_to,
              "page[limit]": 1000, "sort": "exp_date", "fmt": "json"}
    if trade_from:
        params["filter[tradetime_from]"] = trade_from
    key = os.environ.get("EODHD_API_KEY")
    if key:
        params["api_token"] = key
    rows, latest = [], {}
    for page in range(MAX_PAGES):
        params["page[offset]"] = page * 1000
        code, body = get(URL, dict(params))
        if code != 200:
            return rows, f"EODHD ответил {code}"
        data = (body or {}).get("data") or []
        for r in data:
            a = r.get("attributes") or {}
            c, day = a.get("contract"), str(r.get("id") or "")[-10:]
            if c and (c not in latest or day > latest[c][0]):
                latest[c] = (day, r)
        if len(data) < 1000:
            break
    last_day = max((v[0] for v in latest.values()), default=None)
    rows = [v[1] for v in latest.values() if v[0] == last_day]
    return rows, None


def view(ticker, resolve_by, threshold=None, direction="above", spot=None, spot_date=None, today=None, get=None):
    from mathlib import options_implied as OI
    today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if spot is None:
        from ops import price_snapshot as PS
        snap = PS.lookup(ticker)
        if snap.get("ошибка"):
            return {"ok": False, "причина": snap["ошибка"]}
        spot, spot_date = snap["last_close"], snap["last_date"]
    to = (datetime.date.fromisoformat(resolve_by) + datetime.timedelta(days=75)).isoformat()
    trade_from = (datetime.date.fromisoformat(str(spot_date or today)[:10]) - datetime.timedelta(days=4)).isoformat()
    rows, err = fetch(ticker, today, to, get=get, trade_from=trade_from)
    if err and not rows:
        return {"ok": False, "причина": err}
    res = OI.market_expectation(rows, spot, today, resolve_by, threshold, direction, spot_date=spot_date)
    res["источник"] = "EODHD mp/unicornbay/options/eod (опционы), цена — " + (
        f"снимок сервера на {spot_date}" if spot_date else "передана вызовом")
    res["словами"] = OI.words(res)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="что опционы закладывают к дате идеи")
    ap.add_argument("ticker")
    ap.add_argument("resolve_by")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--direction", default="above", choices=("above", "below"))
    ap.add_argument("--spot", type=float)
    a = ap.parse_args(argv)
    res = view(a.ticker, a.resolve_by, a.threshold, a.direction, a.spot)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
