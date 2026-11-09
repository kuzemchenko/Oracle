# -*- coding: utf-8 -*-
"""ops/multiples_view.py — P/E и P/S компании против её собственной истории (Q-в-цене, 09.11). Только чтение EODHD.

  python3 ops/multiples_view.py FSLR [--years 5]

Отчётность — EODHD fundamentals (Income_Statement и Balance_Sheet quarterly, с датой подачи), цены —
EODHD eod (сырой close). Ключ: в облаке подставляет сеть окружения, на сервере — EODHD_API_KEY.
Расчёт — mathlib/multiple_history."""
import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = "https://eodhd.com/api/"


def _default_get(url, params):
    import requests
    r = requests.get(url, params=params, timeout=60)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def view(ticker, years=5, today=None, get=None):
    from mathlib import multiple_history as MH
    get = get or _default_get
    sym = str(ticker).upper()
    sym = sym if "." in sym else sym + ".US"
    key = {"api_token": os.environ["EODHD_API_KEY"]} if os.environ.get("EODHD_API_KEY") else {}
    today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    frm = (datetime.date.fromisoformat(today) - datetime.timedelta(days=int(365.25 * years) + 30)).isoformat()
    out = {"ok": False, "бумага": sym, "источник": "EODHD fundamentals (квартальная отчётность GAAP, дата подачи) + eod"}
    c1, inc = get(BASE + f"fundamentals/{sym}", {"filter": "Financials::Income_Statement::quarterly", "fmt": "json", **key})
    c2, bal = get(BASE + f"fundamentals/{sym}", {"filter": "Financials::Balance_Sheet::quarterly", "fmt": "json", **key})
    c3, px = get(BASE + f"eod/{sym}", {"from": frm, "to": today, "fmt": "json", **key})
    if 200 not in (c1,) or not isinstance(inc, dict) or not isinstance(bal, dict) or not isinstance(px, list):
        out["причина"] = f"EODHD не отдал данные (коды {c1}/{c2}/{c3})"
        return out
    dates = [str(r["date"])[:10] for r in px if r.get("close") is not None]
    closes = [float(r["close"]) for r in px if r.get("close") is not None]
    shares = MH.quarterly(bal, "commonStockSharesOutstanding")
    pe = MH.history(dates, closes, MH.ttm(MH.quarterly(inc, "netIncome")), shares, years, today)
    ps = MH.history(dates, closes, MH.ttm(MH.quarterly(inc, "totalRevenue")), shares, years, today)
    for r in (pe, ps):
        r.pop("точки", None)
    out.update(ok=pe.get("ok") or ps.get("ok"), pe=pe, ps=ps,
               словами=MH.words("P/E (цена к прибыли за год по отчётности)", pe) + " "
               + MH.words("P/S (цена к выручке за год)", ps))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--years", type=int, default=5)
    a = ap.parse_args(argv)
    res = view(a.ticker, a.years)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
