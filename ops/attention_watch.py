# -*- coding: utf-8 -*-
"""ops/attention_watch.py — датчик растущего внимания толпы к компаниям (Q-тренды, 09.11). Только чтение сети.

  python3 ops/attention_watch.py resolve [--limit N]   → тикер → страница Википедии (reports/attention/pages.json)
  python3 ops/attention_watch.py scan                   → reports/attention/attention_<дата>.json и _latest.json
  python3 ops/attention_watch.py show FSLR              → тренд одной бумаги сейчас
  python3 ops/attention_watch.py forward                → форвард-счёт: что было с ценой ПОСЛЕ сигналов

Источник внимания — просмотры Википедии (Wikimedia REST, ежедневно, без лимитов Google Trends). Имя компании —
EODHD fundamentals General (ключ: в облаке — сеть окружения, на сервере — EODHD_API_KEY). Математика —
mathlib/attention_trend. Сервер гоняет scan ежедневно флагом attention_watch_auto (ops/bot.py SCHEDULED_JOBS);
ежедневные файлы уезжают чекпоинтом в GitHub — так сигналы датированы ДО исхода (П16), и forward считает,
двигает ли толпа бумагу, только по тому, что было записано заранее.
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PAGES = ROOT / "reports" / "attention" / "pages.json"   # пишет сервер; уезжает вечерним чекпоинтом вместе с отчётами
OUT_DIR = ROOT / "reports" / "attention"
UA = "oracle-research/1.0 (attention sensor)"
WM = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{page}/daily/{a}/{b}"
SEARCH = "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=3&srsearch={q}"
SIGNAL_PHASES = ("ранний рост", "разгон", "пик")
DAILY_NEW = 150              # новых бумаг на сопоставление за ночь (бережём лимиты Wikimedia и EODHD)
_STOP = {"inc", "corp", "corporation", "company", "co", "ltd", "plc", "holdings", "group", "the", "sa", "ag", "nv",
         "class", "common", "stock", "limited", "international"}


def _curl(url, timeout=40, tries=4, sleep=time.sleep):
    """GET JSON. Лимит Wikimedia («too many requests») — пауза 3/6/12 с и повтор; потом честная ошибка."""
    last = None
    for i in range(tries):
        p = subprocess.run(["curl", "-sS", "-L", "-m", str(timeout), "-A", UA, url], capture_output=True)
        if p.returncode != 0 or not p.stdout:
            last = (p.stderr or b"").decode("utf-8", "replace")[:200] or f"curl {p.returncode}"
        else:
            body = p.stdout.decode("utf-8", "replace")
            try:
                return json.loads(body)
            except ValueError:
                last = "не JSON: " + body[:80]
                if "too many requests" not in body.lower():
                    break
        if i < tries - 1:
            sleep(3 * 2 ** i)
    raise OSError(last or "сбой запроса")


def _words(s):
    return {w for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if w not in _STOP and len(w) > 1}


def resolve_one(ticker, get=_curl):
    """EODHD имя → поиск Википедии → страница, чьё название делит значимое слово с именем. ETF/фонды — без страницы."""
    sym = str(ticker).upper()
    sym = sym if "." in sym else sym + ".US"
    key = os.environ.get("EODHD_API_KEY")
    url = f"https://eodhd.com/api/fundamentals/{sym}?filter=General&fmt=json" + (f"&api_token={key}" if key else "")
    g = get(url) or {}
    name, typ = g.get("Name"), str(g.get("Type") or "")
    if not name:
        return {"page": None, "почему": "EODHD не дал имя компании"}
    if typ.lower() in ("etf", "fund", "mutual fund", "index"):
        return {"page": None, "name": name, "почему": f"тип {typ} — у фонда нет «страницы толпы»"}
    # поиск Википедии из общего облачного адреса отбивается лимитом — название страницы строим из имени
    # и проверяем, что у страницы есть просмотры (сервис просмотров лимитом не бьёт)
    for title in candidates(name):
        try:
            ds, vs = views(title, days=30, get=get)
        except (OSError, ValueError, KeyError):
            continue
        if vs and sum(vs) > 0:
            return {"page": title, "name": name, "type": typ, "как": "имя компании → название страницы, просмотры есть"}
    return {"page": None, "name": name, "почему": "страница с таким именем в Википедии не нашлась"}


_LEGAL = re.compile(r"[,.]?\s+(inc|incorporated|corp|corporation|co|ltd|limited|plc|n\.?v|s\.?a|a\.?g|class [a-z]|"
                    r"common stock|ordinary shares|ads|adr)\.?$", re.I)
_GROUP = re.compile(r"[,.]?\s+(holdings?|group|company|technologies|international)\.?$", re.I)


def candidates(name):
    """«First Solar, Inc.» → First_Solar…; «DHT Holdings Inc» → DHT_Holdings, затем DHT_(company)…"""
    full = re.sub(r"\s+", " ", str(name or "")).strip()
    legal = full
    for _ in range(3):
        legal = _LEGAL.sub("", legal).strip(" ,.")
    short = _GROUP.sub("", legal).strip(" ,.")
    out = []
    # «NVIDIA» в Википедии — «Nvidia»: слова КАПСОМ длиннее 4 букв пробуем и с заглавной
    soft = " ".join(w.capitalize() if w.isupper() and len(w) > 4 else w for w in legal.split())
    for c in (legal, soft, legal + " (company)", short + " (company)", short):
        t = c.strip().replace(" ", "_")
        if t and t not in out:
            out.append(t)
    return out


def resolve(tickers, pages_path=None, get=_curl, pause=0.2):
    p = pathlib.Path(pages_path or PAGES)
    cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    today = datetime.date.today().isoformat()
    for t in tickers:
        if t in cur:
            continue
        try:
            r = resolve_one(t, get=get)
        except (OSError, ValueError) as e:
            r = {"page": None, "почему": f"сбой: {str(e)[:80]}"}
        r["проверено"] = today
        cur[t] = r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        time.sleep(pause)
    return cur


def views(page, days=100, today=None, get=_curl):
    end = datetime.date.fromisoformat(str(today)[:10]) if today else datetime.date.today() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=days)
    d = get(WM.format(page=urllib.parse.quote(page, safe=""), a=start.strftime("%Y%m%d"), b=end.strftime("%Y%m%d")))
    items = (d or {}).get("items") or []
    return [f"{x['timestamp'][:4]}-{x['timestamp'][4:6]}-{x['timestamp'][6:8]}" for x in items], \
        [x.get("views", 0) for x in items]


def show(ticker, pages_path=None, get=_curl):
    from mathlib import attention_trend as AT
    pages = json.loads(pathlib.Path(pages_path or PAGES).read_text(encoding="utf-8")) if pathlib.Path(
        pages_path or PAGES).exists() else {}
    sym = str(ticker).upper()
    sym = sym if "." in sym else sym + ".US"
    rec = pages.get(sym) or resolve_one(sym, get=get)
    if not rec.get("page"):
        return {"ok": False, "бумага": sym, "причина": rec.get("почему") or "страницы нет"}
    ds, vs = views(rec["page"], get=get)
    res = AT.trend(vs, ds)
    res.update(бумага=sym, страница=rec["page"], имя=rec.get("name"), словами=AT.words(rec.get("name") or sym, res),
               источник="просмотры Википедии (Wikimedia REST, пользователи) + mathlib/attention_trend")
    return res


def scan(pages_path=None, out_dir=None, get=_curl, today=None, pause=0.1):
    from mathlib import attention_trend as AT
    pages = json.loads(pathlib.Path(pages_path or PAGES).read_text(encoding="utf-8"))
    day = str(today or datetime.date.today().isoformat())[:10]
    rows, errors = [], 0
    for sym, rec in sorted(pages.items()):
        if not rec.get("page"):
            continue
        try:
            ds, vs = views(rec["page"], get=get)
        except (OSError, ValueError, KeyError):
            errors += 1
            continue
        t = AT.trend(vs, ds)
        if t.get("ok"):
            rows.append({"бумага": sym, "страница": rec["page"], **{k: t[k] for k in (
                "фаза", "ускорение", "ускорение_неделей_раньше", "дней_выше_нормы", "норма_в_день", "за_7_дней_в_день",
                "от_максимума")}, "по_дату": t.get("по_дату")})
        time.sleep(pause)
    rows.sort(key=lambda r: -r["ускорение"])
    rep = {"дата": day, "страниц": sum(1 for r in pages.values() if r.get("page")), "измерено": len(rows),
           "сбоев": errors, "сигналы": [r for r in rows if r["фаза"] in SIGNAL_PHASES], "все": rows,
           "метод": "mathlib/attention_trend: 7 дней против медианы 56 дней до них"}
    d = pathlib.Path(out_dir or OUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(rep, ensure_ascii=False, indent=1) + "\n"
    (d / f"attention_{day}.json").write_text(txt, encoding="utf-8")
    (d / "attention_latest.json").write_text(txt, encoding="utf-8")
    return rep


def forward(out_dir=None, closes_path=None, horizons=(5, 20), bench="SPY.US"):
    """Форвард-счёт: первый день сигнала по бумаге (из датированных файлов) → ход через 5/20 торговых дней против SPY.
    Только записанное ДО исхода; никакой подгонки порогов по итогам."""
    d = pathlib.Path(out_dir or OUT_DIR)
    snap = json.loads(pathlib.Path(closes_path or ROOT / "reports" / "prices" / "closes_latest.json").read_text("utf-8"))
    sy = snap.get("symbols") or {}
    first = {}
    # быстрые следы (ops/attention_fast): новости 15 мин / Википедия по часам / объём торгов — тем же счётом
    fast = d / "fast_signals.jsonl"
    if fast.exists():
        for ln in fast.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            key = (r.get("бумага"), "быстро:" + str(r.get("след")))
            if key[0] and key not in first:
                first[key] = str(r.get("ts"))[:10]
    for f in sorted(d.glob("attention_20*.json")):
        rep = json.loads(f.read_text(encoding="utf-8"))
        for s in rep.get("сигналы") or []:
            key = (s["бумага"], s["фаза"])
            if key not in first:
                first[key] = rep["дата"]

    def ret(sym, day, h):
        rows = sy.get(sym) or []
        idx = next((i for i, r in enumerate(rows) if r[0] > day), None)
        if idx is None or idx + h - 1 >= len(rows) or idx == 0:
            return None
        return rows[idx + h - 1][1] / rows[idx - 1][1] - 1

    out = {}
    for (sym, phase), day in first.items():
        for h in horizons:
            a, b = ret(sym, day, h), ret(bench, day, h)
            if a is None or b is None:
                continue
            o = out.setdefault(f"{phase}|{h}д", {"n": 0, "сумма": 0.0, "выше_рынка": 0})
            o["n"] += 1
            o["сумма"] += a - b
            o["выше_рынка"] += int(a > b)
    for o in out.values():
        o["средний_ход_сверх_рынка"] = round(o.pop("сумма") / o["n"], 4)
    return {"сигналов": len(first), "по_фазам": out,
            "оговорка": "мало наблюдений — вывода нет" if sum(o["n"] for o in out.values()) < 40 else ""}


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("tickers", nargs="*")
    sub.add_parser("scan")
    sub.add_parser("daily", help="сервер: досопоставить до 150 новых бумаг + обход (флаг attention_watch_auto)")
    s = sub.add_parser("show")
    s.add_argument("ticker")
    sub.add_parser("forward")
    a = ap.parse_args(argv)
    if a.cmd == "resolve":
        t = a.tickers or sorted((json.loads((ROOT / "reports" / "prices" / "closes_latest.json").read_text("utf-8"))
                                 .get("symbols") or {}).keys())
        res = resolve(t[:a.limit] if a.limit else t)
        print(sum(1 for v in res.values() if v.get("page")), "страниц из", len(res))
    elif a.cmd == "daily":
        syms = sorted((json.loads((ROOT / "reports" / "prices" / "closes_latest.json").read_text("utf-8"))
                       .get("symbols") or {}).keys())
        have = json.loads(PAGES.read_text("utf-8")) if PAGES.exists() else {}
        resolve([t for t in syms if t not in have][:DAILY_NEW], pause=1.0)
        rep = scan(pause=0.5)
        print(rep["измерено"], "измерено,", len(rep["сигналы"]), "сигналов,", rep["сбоев"], "сбоев")
    elif a.cmd == "scan":
        rep = scan()
        print(rep["измерено"], "измерено,", len(rep["сигналы"]), "сигналов,", rep["сбоев"], "сбоев")
    elif a.cmd == "show":
        print(json.dumps(show(a.ticker), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(forward(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
