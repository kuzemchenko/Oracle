# -*- coding: utf-8 -*-
"""ops/history_prices.py — длинная история закрытий для проверки исторических каскадов (облако).

Источники (сеть облака открыта владельцем 03.11): Yahoo Finance chart API (акции/ETF/фьючерсы/индексы,
скорректированное закрытие — сплиты и дивиденды учтены) и FRED (серии сырья: FRED:DCOILBRENTEU и т.п.).
Серверная база EODHD хранит лишь недавнюю историю, а каскадам нужны десятилетия.
Кэш — .cache/history/<символ>.json (вне git); --refresh перекачивает.
Только чтение внешних данных; журналы не трогает. Числа считает mathlib/event_study (Инв#6).

  python3 ops/history_prices.py FRO CL=F FRED:DCOILBRENTEU     → сводка: с какой даты, сколько дней
"""
import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CACHE = ROOT / ".cache" / "history"
UA = "Mozilla/5.0 (oracle research)"
_LAST = [0.0]


def norm(sym):
    """'FRO.US'/'fro' → 'FRO'; 'BRK.B' → 'BRK-B' (Yahoo); 'FRED:X' и 'CL=F', '^GSPC' — как есть."""
    s = str(sym or "").strip()
    if not s or s == "нет":
        return None
    if s.upper().startswith("FRED:"):
        return "FRED:" + s[5:].strip().upper()
    s = s.upper()
    if s.endswith(".US"):
        s = s[:-3]
    if "=" not in s and not s.startswith("^"):
        s = s.replace(".", "-")
    return s


def _curl(url, timeout=40, tries=4):
    """GET с вежливой паузой (≤ ~3 запросов/с) и повтором при сетевом сбое (2, 4, 8 с)."""
    err = None
    for k in range(tries):
        wait = 0.35 - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.time()
        p = subprocess.run(["curl", "-sS", "--http1.1", "-m", str(timeout), "-A", UA, url],
                           capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
        err = p.stderr.strip()[:200] or f"curl {p.returncode}, пустой ответ"
        if k < tries - 1:
            time.sleep(2 ** (k + 1))
    raise OSError(err)


def _yahoo(sym):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym, safe="")
           + "?period1=0&period2=" + str(int(time.time()) + 86400) + "&interval=1d&events=split%2Cdiv")
    d = json.loads(_curl(url))
    res = (d.get("chart") or {}).get("result")
    if not res:
        err = (d.get("chart") or {}).get("error") or {}
        raise LookupError(f"Yahoo: {err.get('description') or 'нет данных'}")
    r = res[0]
    ts = r.get("timestamp") or []
    ind = r.get("indicators") or {}
    adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose")
    cl = ((ind.get("quote") or [{}])[0] or {}).get("close")
    col = adj if adj and any(x is not None for x in adj) else cl
    # дата торгового дня — в часовом поясе биржи на ТУ дату (летнее/зимнее время), а не текущим сдвигом
    meta = r.get("meta") or {}
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
    except Exception:                                    # noqa: BLE001 — без базы поясов: Нью-Йорк ≈ UTC−5
        tz = datetime.timezone(datetime.timedelta(hours=-5))
    rows = []
    for t, c in zip(ts, col or []):
        if c is None:
            continue
        d_ = datetime.datetime.fromtimestamp(t, tz).date().isoformat()
        rows.append([d_, float(c)])
    return rows, ("adjclose" if col is adj else "close")


def _fred(series):
    txt = _curl("https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + urllib.parse.quote(series))
    rows = []
    for ln in txt.splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        try:
            rows.append([parts[0][:10], float(parts[1])])
        except ValueError:
            continue                                # «.» = нет наблюдения
    if not rows:
        raise LookupError("FRED: нет данных")
    return rows, "fred"


def fetch(sym, refresh=False, cache=CACHE):
    """→ {"symbol", "rows": [[дата, закрытие]], "колонка", "источник", "скачано"} или {"ошибка"}."""
    s = norm(sym)
    if not s:
        return {"symbol": sym, "ошибка": "символа нет"}
    cache = pathlib.Path(cache)
    f = cache / (s.replace(":", "_").replace("/", "_").replace("^", "IDX_") + ".json")
    if f.exists() and not refresh:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        if s.startswith("FRED:"):
            rows, col = _fred(s[5:])
            src = "FRED " + s[5:]
        else:
            rows, col = _yahoo(s)
            src = "Yahoo Finance " + s
    except (OSError, LookupError, ValueError) as e:
        return {"symbol": s, "ошибка": str(e)[:200]}
    out = {"symbol": s, "rows": rows, "колонка": col, "источник": src,
           "скачано": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    cache.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(f)
    return out


def series(sym, refresh=False, cache=CACHE):
    """→ (dates, closes) через mathlib.event_study.clean_series, или None (нет данных)."""
    from mathlib import event_study as ES
    r = fetch(sym, refresh=refresh, cache=cache)
    if r.get("ошибка") or not r.get("rows"):
        return None
    ds, cs = ES.clean_series(r["rows"])
    return (ds, cs) if ds else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="длинная история закрытий (Yahoo/FRED) с кэшем")
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    bad = 0
    for s in a.symbols:
        r = fetch(s, refresh=a.refresh)
        if r.get("ошибка"):
            bad += 1
            print(f"{s}: ошибка — {r['ошибка']}")
            continue
        rows = r["rows"]
        print(f"{r['symbol']}: {len(rows)} дней, {rows[0][0]} … {rows[-1][0]} ({r['источник']}, {r['колонка']})")
    return 0 if not bad else 3


if __name__ == "__main__":
    sys.exit(main())
