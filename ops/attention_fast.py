# -*- coding: utf-8 -*-
"""ops/attention_fast.py — БЫСТРЫЕ следы толпы (Q-тренды-быстро, владелец 09.11 «сделай все три»). Только чтение сети.

Сутки Википедии — медленная волна. Здесь три быстрых сборщика (сервер, каждые 15 минут, флаг attention_fast_auto):
  1) НОВОСТИ — файл GDELT GKG за 15 минут (каждая статья мира с перечнем упомянутых организаций): сколько статей
     упомянули компанию; всплеск = последние 2 часа против медианы 2-часовых окон за прошлые 3 дня;
  2) ВИКИПЕДИЯ ПО ЧАСАМ — почасовой файл просмотров Wikimedia (выходит с отставанием ~3 ч, читается потоком,
     берём только наши страницы); всплеск = последние 3 часа против тех же часов за прошлые 7 дней;
  3) ОБЪЁМ ТОРГОВ — EODHD real-time пачками (во время сессии Нью-Йорка): темп объёма против обычного дня;
     для бумаг со всплеском — объём опционов за последний день против среднего за 10 дней (EODHD Unicorn Bay).
Сырьё — reports/attention/fast/<вид>_<дата>.jsonl; сигналы — reports/attention/fast_latest.json и
fast_signals.jsonl (датированы ДО исхода — их считает форвард, как и медленную волну). Математика —
mathlib/attention_trend (surge, volume_pace). LLM нет.

  python3 ops/attention_fast.py tick       → сбор (что пора) + сигналы
  python3 ops/attention_fast.py signals    → только пересчёт сигналов
"""
import argparse
import datetime
import gzip
import io
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
FAST = ROOT / "reports" / "attention" / "fast"
LATEST = ROOT / "reports" / "attention" / "fast_latest.json"
SIGNALS_LOG = ROOT / "reports" / "attention" / "fast_signals.jsonl"
GDELT = "http://data.gdeltproject.org/gdeltv2/"
WIKI_HOURLY = "https://dumps.wikimedia.org/other/pageviews/{y}/{y}-{m}/pageviews-{ymd}-{h}0000.gz"
UA = "oracle-research/1.0 (attention sensor)"
_LEGAL = re.compile(r"[,.]?\s+(inc|incorporated|corp|corporation|co|ltd|limited|plc|n\.?v|s\.?a|a\.?g|holdings?|group|"
                    r"company|class [a-z]|common stock)\.?$", re.I)


def _curl_bytes(url, timeout=120):
    p = subprocess.run(["curl", "-sSL", "-m", str(timeout), "-A", UA, url], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise OSError((p.stderr or b"").decode("utf-8", "replace")[:200] or f"curl {p.returncode}")
    return p.stdout


def _append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read(path):
    try:
        return [json.loads(x) for x in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _recent(kind, days, today=None):
    d = datetime.date.fromisoformat(str(today)[:10]) if today else datetime.datetime.now(datetime.timezone.utc).date()
    rows = []
    for i in range(days, -1, -1):
        rows += _read(FAST / f"{kind}_{(d - datetime.timedelta(days=i)).isoformat()}.jsonl")
    return rows


def watchlist(pages_path=None):
    """Бумаги с найденной страницей → {тикер: {"page", "org": имя для GDELT}}."""
    from ops import attention_watch as AW
    p = pathlib.Path(pages_path or AW.PAGES)
    pages = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    out = {}
    for t, r in pages.items():
        if r.get("page"):
            org = str(r.get("name") or r["page"].replace("_", " "))
            for _ in range(3):
                org = _LEGAL.sub("", org).strip(" ,.")
            out[t] = {"page": r["page"], "org": org.lower()}
    return out


# ── 1) новости: GDELT GKG по 15 минут ────────────────────────────────────────────────────
def gkg_counts(raw_zip, orgs):
    """Байты .gkg.csv.zip → (статей, {тикер: статей с упоминанием организации}). Поле V1Organizations (13)."""
    z = zipfile.ZipFile(io.BytesIO(raw_zip))
    text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    by_org = {}
    for t, o in orgs.items():
        if len(o) >= 3:
            by_org.setdefault(o, []).append(t)
    total, counts = 0, {}
    for ln in text.splitlines():
        f = ln.split("\t")
        if len(f) < 14:
            continue
        total += 1
        seen = {x.split(",")[0].strip().lower() for x in f[13].split(";") if x}
        for o in seen & by_org.keys():
            for t in by_org[o]:
                counts[t] = counts.get(t, 0) + 1
    return total, counts


def news_tick(wl, get=_curl_bytes, state=None):
    last = get(GDELT + "lastupdate.txt", 30).decode("utf-8", "replace")
    url = next((ln.split()[-1] for ln in last.splitlines() if ln.strip().endswith(".gkg.csv.zip")), None)
    if not url:
        return None
    stamp = url.rsplit("/", 1)[-1][:14]
    if state is not None and state.get("news") == stamp:
        return None
    total, counts = gkg_counts(get(url), {t: w["org"] for t, w in wl.items()})
    rec = {"stamp": stamp, "статей": total, "упоминания": counts}
    _append(FAST / f"news_{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}.jsonl", rec)
    if state is not None:
        state["news"] = stamp
    return rec


# ── 2) Википедия по часам ─────────────────────────────────────────────────────────────────
def wiki_hour_tick(wl, now=None, get=None, state=None):
    """Последний вышедший почасовой файл (пробуем 3–5 часов назад), потоком: только строки наших страниц."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    want = {w["page"]: t for t, w in wl.items()}
    for lag in (3, 4, 5):
        h = (now - datetime.timedelta(hours=lag)).replace(minute=0, second=0, microsecond=0)
        key = h.strftime("%Y%m%d%H")
        if state is not None and state.get("wiki") and state["wiki"] >= key:
            return None
        url = WIKI_HOURLY.format(y=h.year, m=f"{h.month:02d}", ymd=h.strftime("%Y%m%d"), h=f"{h.hour:02d}")
        try:
            counts = (get or _wiki_stream)(url, want)
        except OSError:
            continue
        rec = {"hour": h.strftime("%Y-%m-%dT%H"), "просмотры": counts}
        _append(FAST / f"wiki_{h.date().isoformat()}.jsonl", rec)
        if state is not None:
            state["wiki"] = key
        return rec
    return None


def _wiki_stream(url, want):
    p = subprocess.Popen(["curl", "-sSLf", "-m", "300", "-A", UA, url], stdout=subprocess.PIPE)
    counts = {}
    with gzip.GzipFile(fileobj=p.stdout) as g:
        for ln in g:
            if not ln.startswith(b"en "):
                continue
            parts = ln.split(b" ")
            if len(parts) >= 3:
                t = want.get(parts[1].decode("utf-8", "replace"))
                if t:
                    counts[t] = counts.get(t, 0) + int(parts[2] or 0)
    if p.wait() != 0:
        raise OSError("почасового файла ещё нет")
    return counts


# ── 3) объём торгов внутри дня ────────────────────────────────────────────────────────────
def session_minutes(now):
    """Минут с открытия Нью-Йорка (9:30 ET) или None вне сессии. Летнее/зимнее время — по правилу США."""
    y = now.year
    dst_start = datetime.datetime(y, 3, 8, tzinfo=datetime.timezone.utc)
    dst_start += datetime.timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end = datetime.datetime(y, 11, 1, tzinfo=datetime.timezone.utc)
    dst_end += datetime.timedelta(days=(6 - dst_end.weekday()) % 7)
    off = 4 if dst_start <= now < dst_end else 5
    et = now - datetime.timedelta(hours=off)
    if et.weekday() > 4:
        return None
    m = (et.hour * 60 + et.minute) - (9 * 60 + 30)
    return m if 0 <= m <= 390 else None


def volume_tick(tickers, now=None, get=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    mins = session_minutes(now)
    if mins is None:
        return None
    key = os.environ.get("EODHD_API_KEY")
    vols = {}
    t = sorted(tickers)
    for i in range(0, len(t), 20):
        batch = t[i:i + 20]
        url = (f"https://eodhd.com/api/real-time/{batch[0]}?fmt=json" + (f"&s={','.join(batch[1:])}" if batch[1:] else "")
               + (f"&api_token={key}" if key else ""))
        try:
            data = json.loads((get or _curl_bytes)(url, 60))
        except (OSError, ValueError):
            continue
        for r in data if isinstance(data, list) else [data]:
            if isinstance(r, dict) and r.get("code") and isinstance(r.get("volume"), (int, float)):
                vols[r["code"]] = r["volume"]
    rec = {"ts": now.strftime("%Y-%m-%dT%H:%M"), "минут_сессии": mins, "объём": vols}
    _append(FAST / f"volume_{now.date().isoformat()}.jsonl", rec)
    return rec


def adv(db=None, days=20):
    """Обычный дневной объём: среднее за 20 торговых дней по серверной базе (quotes.volume)."""
    p = pathlib.Path(db or ROOT / "storage" / "oracle.db")
    if not p.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
        rows = con.execute("SELECT symbol, AVG(volume) FROM (SELECT symbol, volume, ROW_NUMBER() OVER (PARTITION BY "
                           "symbol ORDER BY date DESC) rn FROM quotes WHERE volume > 0) WHERE rn <= ? GROUP BY symbol",
                           (days,)).fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {s: v for s, v in rows if v}


def options_volume(ticker, get=None, today=None):
    """Объём опционов за последний день против среднего за предыдущие 10 дней (EODHD Unicorn Bay)."""
    today = today or datetime.date.today()
    key = os.environ.get("EODHD_API_KEY")
    sym = str(ticker).split(".")[0]
    url = (f"https://eodhd.com/api/mp/unicornbay/options/eod?filter%5Bunderlying_symbol%5D={sym}"
           f"&filter%5Btradetime_from%5D={(today - datetime.timedelta(days=16)).isoformat()}&page%5Blimit%5D=1000&fmt=json"
           + (f"&api_token={key}" if key else ""))
    by_day = {}
    for page in range(6):
        try:
            d = json.loads((get or _curl_bytes)(url + f"&page%5Boffset%5D={page * 1000}", 60))
        except (OSError, ValueError):
            break
        rows = d.get("data") or []
        for r in rows:
            day = str(r.get("id") or "")[-10:]
            v = (r.get("attributes") or {}).get("volume")
            if isinstance(v, (int, float)):
                by_day[day] = by_day.get(day, 0) + v
        if len(rows) < 1000:
            break
    days = sorted(by_day)
    if len(days) < 5:
        return {"ok": False, "причина": "мало дней с объёмом опционов"}
    last, prev = by_day[days[-1]], [by_day[x] for x in days[-11:-1]]
    base = sum(prev) / len(prev)
    return {"ok": True, "день": days[-1], "объём": last, "обычно": round(base), "кратно": round(last / base, 2) if base else None}


# ── сигналы ───────────────────────────────────────────────────────────────────────────────
def signals(wl, now=None, adv_map=None, options_fn=None, write=True):
    from mathlib import attention_trend as AT
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    out = {}
    # 1) новости: последние 8 файлов (2 часа) против 2-часовых окон за прошлые 3 дня
    news = sorted(_recent("news", 3, today), key=lambda r: r["stamp"])
    if len(news) >= 16:
        cur = news[-8:]
        hist = news[:-8]
        windows = [hist[i:i + 8] for i in range(0, len(hist) - 7, 8)]
        for t in wl:
            r = AT.surge(sum(x["упоминания"].get(t, 0) for x in cur),
                         [sum(x["упоминания"].get(t, 0) for x in w) for w in windows])
            if r.get("всплеск"):
                out.setdefault(t, {})["новости"] = r
    # 2) Википедия по часам: последние 3 часа против тех же часов за прошлые 7 дней
    wiki = sorted(_recent("wiki", 7, today), key=lambda r: r["hour"])
    if len(wiki) >= 3:
        last3 = wiki[-3:]
        hours = {x["hour"][-2:] for x in last3}
        days = {x["hour"][:10] for x in last3}
        hist = {}
        for x in wiki[:-3]:
            if x["hour"][-2:] in hours and x["hour"][:10] not in days:
                hist.setdefault(x["hour"][:10], []).append(x)
        for t in wl:
            r = AT.surge(sum(x["просмотры"].get(t, 0) for x in last3),
                         [sum(x["просмотры"].get(t, 0) for x in rows) for rows in hist.values()], min_recent=30)
            if r.get("всплеск"):
                out.setdefault(t, {})["википедия_часы"] = r
    # 3) объём торгов: последний замер сегодня против обычного дня
    vol = _read(FAST / f"volume_{today}.jsonl")
    if vol:
        last = vol[-1]
        am = adv_map if adv_map is not None else adv()
        for t, v in last["объём"].items():
            r = AT.volume_pace(v, am.get(t), last["минут_сессии"])
            if r.get("всплеск"):
                out.setdefault(t, {})["объём_торгов"] = r
    # опционы — только для бумаг со всплеском (бережём лимиты)
    for t in sorted(out, key=lambda x: -len(out[x]))[:10]:
        try:
            o = (options_fn or options_volume)(t)
        except Exception:                                        # noqa: BLE001
            o = {"ok": False}
        if o.get("ok"):
            out[t]["опционы"] = o
    rep = {"ts": now.strftime("%Y-%m-%dT%H:%M"), "сигналов": len(out),
           "сигналы": [{"бумага": t, "следы": sorted(v), **v} for t, v in sorted(out.items(), key=lambda kv: -len(kv[1]))],
           "метод": "новости GDELT 2 ч против 3 дней; Википедия 3 ч против тех же часов 7 дней; объём — темп к обычному дню; "
                    "порог ×3 (mathlib/attention_trend)"}
    if write:
        LATEST.parent.mkdir(parents=True, exist_ok=True)
        LATEST.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        logged = {(r.get("бумага"), r.get("след")) for r in _read(SIGNALS_LOG) if str(r.get("ts", ""))[:10] == today}
        for s in rep["сигналы"]:
            for trace in s["следы"]:
                if trace != "опционы" and (s["бумага"], trace) not in logged:
                    _append(SIGNALS_LOG, {"ts": rep["ts"], "бумага": s["бумага"], "след": trace,
                                          "кратно": (s[trace].get("кратно") or s[trace].get("темп"))})
    return rep


STATE = FAST / "state.json"


def tick(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    wl = watchlist()
    if not wl:
        return {"причина": "нет сопоставленных страниц (сначала ops/attention_watch.py daily)"}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    done = {}
    for name, fn in (("news", lambda: news_tick(wl, state=st)), ("volume", lambda: volume_tick(list(wl), now)),
                     ("wiki", lambda: wiki_hour_tick(wl, now, state=st))):
        try:
            done[name] = bool(fn())
        except Exception as e:                                   # noqa: BLE001 — один источник ≠ весь сбор
            done[name] = f"сбой: {type(e).__name__}"
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")
    rep = signals(wl, now)
    return {"собрано": done, "сигналов": rep["сигналов"]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("tick", "signals"))
    a = ap.parse_args(argv)
    if a.cmd == "tick":
        print(json.dumps(tick(), ensure_ascii=False))
    else:
        print(json.dumps(signals(watchlist()), ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
