#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/gdelt.py — коннектор GDELT DOC 2.0 (MASTER_SPEC §30 п.1: «GDELT — основа, бесплатно,
мультиязычность для П1 и библиотек §23»).

GDELT DOC API: без ключа, мультиязычный, отдаёт artlist (url, title, seendate, domain,
language, sourcecountry). Тела статьи нет — это нормально (§4: теги, не контент). Запросы
формируются из config/news.yaml (темы → ключевые слова + языки). Нормализация, тегирование
и дедуп — в data/news_common.py. Идемпотентен: повторный заход дозаливает по id (канонический URL).

GDELT строго лимитирует частоту (HTTP 429) — встроен backoff с паузами между запросами.

Запуск:
    set -a && . ./.env && set +a            # ключ не нужен, но единообразно
    .venv/bin/python data/gdelt.py                 # все темы из news.yaml за timespan
    .venv/bin/python data/gdelt.py --query "oil OR opec" --lang english --timespan 1d
    .venv/bin/python data/gdelt.py --status
"""
import sys
import time
import json
import argparse
import urllib.parse
import urllib.request
import urllib.error
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import news_common as nc

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEWS_CFG = ROOT / "config" / "news.yaml"
API = "https://api.gdeltproject.org/api/v2/doc/doc"
UA = "oracle/1.0 (research news collector)"

# GDELT принимает язык как полное имя (sourcelang:english) — наши news.yaml хранят ISO 639-1,
# переводим обратно для запроса.
ISO1_TO_GDELT_LANG = {
    "en": "english", "es": "spanish", "ru": "russian", "ar": "arabic",
    "zh": "chinese", "fr": "french", "de": "german", "pt": "portuguese",
    "it": "italian", "nl": "dutch", "pl": "polish", "tr": "turkish",
    "ja": "japanese", "ko": "korean", "uk": "ukrainian", "fa": "persian",
    "he": "hebrew", "hi": "hindi", "id": "indonesian", "vi": "vietnamese",
}


def _get(url, timeout=45, tries=4, pause=8.0, max_wait=60.0):
    """GET с backoff на 429/таймаут. GDELT free Doc API жёстко лимитирует частоту (≈1 запрос/5 c
    на IP) и при перегрузе банит окном. Стратегия: УВАЖАЕМ заголовок Retry-After (если прислан и
    разумен), иначе ЭКСПОНЕНЦИАЛЬНАЯ пауза 8→16→32 c (потолок max_wait) — линейные 5·i были
    слишком короткими (оттого «недоступен после 5 попыток» в cron.log), а слишком длинные вешают
    живой прогон. GDELT — best-effort (основной поток новостей несёт NewsAPI.ai): после tries
    попыток отдаём управление, run_all ловит и идёт к следующему запросу (Нед.10)."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 502, 503, 504):
                ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                try:
                    wait = float(ra) if ra else None
                except (TypeError, ValueError):
                    wait = None
                if wait is None or wait > max_wait:
                    wait = min(pause * (2 ** i), max_wait)   # игнорируем неадекватно длинный Retry-After
                time.sleep(wait)
                continue
            raise RuntimeError(f"GDELT HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001 — сеть/таймаут → backoff
            last = e
            time.sleep(min(pause * (2 ** i), max_wait))
    raise RuntimeError(f"GDELT недоступен после {tries} попыток: {last}")


def _strip_outer_parens(q):
    """Снять ровно одну полностью обрамляющую пару скобок, чтобы не получить '((...))':
    GDELT парсит двойные скобки как пустой запрос и молча отдаёт 0 статей."""
    q = q.strip()
    if len(q) >= 2 and q[0] == "(" and q[-1] == ")":
        depth = 0
        for i, ch in enumerate(q):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    # скобка закрывается раньше конца — это не обрамляющая пара
                    return q if i != len(q) - 1 else q[1:-1].strip()
    return q


def fetch(query, lang=None, timespan="1d", maxrecords=250):
    """Один запрос artlist к GDELT. lang — ISO 639-1 (мы переведём в имя для GDELT).
    Запрос всегда оборачивается РОВНО в одни скобки + sourcelang."""
    inner = _strip_outer_parens(query)
    if lang:
        gl = ISO1_TO_GDELT_LANG.get(lang, lang)
        q = f"({inner}) sourcelang:{gl}"
    else:
        q = f"({inner})"
    params = {
        "query": q,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(maxrecords),
        "timespan": timespan,
        "sort": "datedesc",
    }
    url = API + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    body = _get(url)
    if not body.strip():
        return []
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        # GDELT иногда отдаёт текст ошибки вместо JSON — это не статьи.
        return []
    return d.get("articles", []) or []


def to_records(articles):
    out = []
    for a in articles:
        url = a.get("url") or a.get("url_mobile")
        if not url:
            continue
        out.append(nc.make_record(
            source="gdelt",
            url=url,
            title=a.get("title"),
            published_at=nc.parse_gdelt_time(a.get("seendate")),
            lang=a.get("language"),
            country=a.get("sourcecountry"),
            domain=a.get("domain"),
            body=None,
            raw=a,
        ))
    return out


def load_queries():
    import yaml
    with open(NEWS_CFG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    gd = cfg.get("gdelt", {})
    timespan = gd.get("timespan", "1d")
    maxrecords = int(gd.get("maxrecords", 250))
    langs = gd.get("langs") or [None]
    jobs = []
    for theme in cfg.get("themes", []):
        name = theme["name"]
        query = theme["gdelt_query"]
        for lang in langs:
            jobs.append((name, query, lang))
    return jobs, timespan, maxrecords, float(gd.get("pause_sec", 3.0))


def run_all(con, verbose=True):
    jobs, timespan, maxrecords, pause = load_queries()
    total_new = total_seen = 0
    failures = 0
    for i, (theme, query, lang) in enumerate(jobs):
        try:
            arts = fetch(query, lang=lang, timespan=timespan, maxrecords=maxrecords)
            recs = to_records(arts)
            new, seen = nc.upsert_news(con, recs)
            total_new += new
            total_seen += seen
            if verbose:
                print(f"✅ GDELT {theme:10} lang={lang or 'all':8} +{new:4d} новых / {seen:4d} получено")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"❌ GDELT {theme:10} lang={lang}: {e}", file=sys.stderr)
        if i < len(jobs) - 1:
            time.sleep(pause)  # бережём лимит GDELT
    return total_new, total_seen, failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="разовый запрос GDELT (минуя news.yaml)")
    ap.add_argument("--lang", help="ISO 639-1, напр. en/ru/ar")
    ap.add_argument("--timespan", default="1d")
    ap.add_argument("--maxrecords", type=int, default=250)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    con = nc.db_connect()
    if args.status:
        nc.status(con)
        return 0

    if args.query:
        arts = fetch(args.query, lang=args.lang, timespan=args.timespan, maxrecords=args.maxrecords)
        new, seen = nc.upsert_news(con, to_records(arts))
        print(f"✅ GDELT разовый запрос: +{new} новых / {seen} получено")
        nc.dedupe_recent(con)
        nc.status(con)
        return 0

    new, seen, fails = run_all(con)
    print(f"\nGDELT итог: +{new} новых строк / {seen} получено, сбоев {fails}")
    nc.dedupe_recent(con)
    nc.status(con)
    return 1 if fails and new == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
