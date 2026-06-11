#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/newsapi_ai.py — коннектор NewsAPI.ai / EventRegistry (MASTER_SPEC §30 п.1:
«NewsAPI.ai/EventRegistry (~$90/мес, тегированный поток)»).

getArticles по $query: ключевые слова + язык(и) из config/news.yaml. Источник уже отдаёт
готовые теги — lang (ISO 639-3), dataType (news/blog/pr → тип источника), dateTimePub (время),
source.location (страна, когда известна), isDuplicate (свой флаг дубля), eventUri (кластер
события для будущего §6). Нормализация в общий вид и дедуп — в data/news_common.py.
Идемпотентен: дозаливка по id (канонический URL).

Запуск:
    set -a && . ./.env && set +a            # нужен NEWSAPI_AI_KEY
    .venv/bin/python data/newsapi_ai.py            # все темы из news.yaml
    .venv/bin/python data/newsapi_ai.py --keyword oil --lang eng --pages 1
    .venv/bin/python data/newsapi_ai.py --status
"""
import os
import sys
import time
import json
import argparse
import urllib.request
import urllib.error
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import news_common as nc

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEWS_CFG = ROOT / "config" / "news.yaml"
API = "https://eventregistry.org/api/v1/article/getArticles"


def _post(payload, timeout=60, tries=4, pause=4.0):
    last = None
    data = json.dumps(payload).encode("utf-8")
    for i in range(tries):
        try:
            req = urllib.request.Request(API, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 502, 503, 504):
                time.sleep(pause * (i + 1))
                continue
            raise RuntimeError(f"NewsAPI.ai HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(pause * (i + 1))
    raise RuntimeError(f"NewsAPI.ai недоступен после {tries} попыток: {last}")


def _country_from_source(src):
    """source.location.country.label.eng → имя страны, если NewsAPI.ai его дал; иначе None."""
    if not isinstance(src, dict):
        return None
    loc = src.get("location")
    if not isinstance(loc, dict):
        return None
    country = loc.get("country")
    if isinstance(country, dict):
        label = country.get("label")
        if isinstance(label, dict):
            return label.get("eng") or label.get("eng".upper())
        if isinstance(label, str):
            return label
    # некоторые места — сами страны (type=country)
    if loc.get("type") == "country":
        label = loc.get("label")
        if isinstance(label, dict):
            return label.get("eng")
        if isinstance(label, str):
            return label
    return None


def fetch_page(api_key, keyword, lang, page=1, count=100, sort="date"):
    payload = {
        "query": {"$query": {"$and": [
            {"keyword": keyword, "keywordLoc": "body,title"},
            {"lang": lang},
        ]}},
        "resultType": "articles",
        "articlesSortBy": sort,
        "articlesCount": count,
        "articlesPage": page,
        "includeArticleLocation": True,
        "includeSourceLocation": True,
        "dataType": ["news", "blog", "pr"],
        "apiKey": api_key,
    }
    d = _post(payload)
    arts = d.get("articles", {})
    return arts.get("results", []) or [], int(arts.get("totalResults", 0) or 0)


def to_records(results):
    out = []
    for a in results:
        url = a.get("url")
        if not url:
            continue
        src = a.get("source") or {}
        published = nc.parse_iso_time(a.get("dateTimePub") or a.get("dateTime"))
        out.append(nc.make_record(
            source="newsapi_ai",
            url=url,
            title=a.get("title"),
            published_at=published,
            lang=a.get("lang"),
            country=_country_from_source(src),
            domain=src.get("uri"),
            body=a.get("body"),
            datatype=a.get("dataType") or src.get("dataType"),
            event_uri=a.get("eventUri"),
            provider_duplicate=bool(a.get("isDuplicate")),
            raw=a,
        ))
    return out


def load_queries():
    import yaml
    with open(NEWS_CFG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    na = cfg.get("newsapi_ai", {})
    langs = na.get("langs", ["eng"])
    pages = int(na.get("pages", 1))
    count = int(na.get("count", 100))
    pause = float(na.get("pause_sec", 1.0))
    jobs = []
    for theme in cfg.get("themes", []):
        kw = theme.get("newsapi_keyword") or theme.get("name")
        for lang in langs:
            jobs.append((theme["name"], kw, lang))
    return jobs, langs, pages, count, pause


def run_all(con, api_key, verbose=True):
    jobs, _langs, pages, count, pause = load_queries()
    total_new = total_seen = 0
    failures = 0
    for theme, kw, lang in jobs:
        got_any = False
        for page in range(1, pages + 1):
            try:
                results, total = fetch_page(api_key, kw, lang, page=page, count=count)
                if not results:
                    break
                new, seen = nc.upsert_news(con, to_records(results))
                total_new += new
                total_seen += seen
                got_any = True
                if verbose:
                    print(f"✅ NewsAPI {theme:10} {lang} p{page}: +{new:4d} новых / {seen:4d} (всего по теме ~{total})")
                if page * count >= total:
                    break
                time.sleep(pause)
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"❌ NewsAPI {theme:10} {lang} p{page}: {e}", file=sys.stderr)
                break
        if not got_any and verbose:
            print(f"·  NewsAPI {theme:10} {lang}: пусто")
        time.sleep(pause)
    return total_new, total_seen, failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword")
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    con = nc.db_connect()
    if args.status:
        nc.status(con)
        return 0

    api_key = os.environ.get("NEWSAPI_AI_KEY")
    if not api_key:
        print("ОШИБКА: NEWSAPI_AI_KEY не задан (source .env)", file=sys.stderr)
        return 1

    if args.keyword:
        total_new = total_seen = 0
        for page in range(1, args.pages + 1):
            results, total = fetch_page(api_key, args.keyword, args.lang, page=page, count=args.count)
            if not results:
                break
            new, seen = nc.upsert_news(con, to_records(results))
            total_new += new
            total_seen += seen
            print(f"✅ NewsAPI '{args.keyword}' {args.lang} p{page}: +{new} / {seen} (всего ~{total})")
            if page * args.count >= total:
                break
        nc.dedupe_recent(con)
        nc.status(con)
        return 0

    new, seen, fails = run_all(con, api_key)
    print(f"\nNewsAPI.ai итог: +{new} новых строк / {seen} получено, сбоев {fails}")
    nc.dedupe_recent(con)
    nc.status(con)
    return 1 if fails and new == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
