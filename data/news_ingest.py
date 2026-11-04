#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/news_ingest.py — суточный сборщик новостного потока (MASTER_SPEC §4 «Сборщик новостей»,
§24 Нед.2, П1). Единая точка входа для cron.

Делает за один проход то, что требует gate Недели 2 — «суточный поток нормализуется и
тегируется (язык, страна, тип, время) без ручной правки, дедупликация работает»:
  1. GDELT       — мультиязычный сбор по темам news.yaml (data/gdelt.py)
  2. NewsAPI.ai  — тегированный поток по темам (data/newsapi_ai.py)
  3. дедуп       — точный (по каноническому URL, на вставке) + near-dup по заголовку за сутки
  4. Google Trends — best-effort (data/trends.py); 429 от Google НЕ валит пайплайн
  5. отчёт качества тегирования — печатает и пишет в journal/news_ingest.log

Новости (источники истины П1) — обязательная часть; тренды — вспомогательная и не блокируют.

Запуск:
    set -a && . ./.env && set +a
    .venv/bin/python data/news_ingest.py            # полный суточный проход
    .venv/bin/python data/news_ingest.py --no-trends
    .venv/bin/python data/news_ingest.py --report   # только отчёт по тому, что в базе
"""
import os
import sys
import json
import argparse
import datetime
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import news_common as nc
import gdelt
import newsapi_ai

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "journal" / "news_ingest.log"
NOTICES = ROOT / "journal" / "notices.jsonl"   # канал заметок владельцу (бот пушит по курсору, как §25)


def source_health_notices(gdelt_res=None, trends_res=None):
    """Разбор 31.10.2026: источники умирали МОЛЧА. GDELT с 26.10 отдавал 0 статей (HTTP 429 на все 21
    запроса/день — бан IP), Google Trends 3,5 месяца не фетчил ни одного ключа реестра «внимания» — а
    сборщик выходил кодом 0 и алерта не было. Здесь — чистая функция: по итогам источников возвращает
    тексты заметок владельцу (пусто = всё здорово). Пишется в notices.jsonl (не alerts: alerts = «прогон
    упал, идей не будет», а это частичная деградация данных при живом конвейере).
    gdelt_res = (new, seen, fails); trends_res = (n_iot, n_rel, rate_limited, njobs)."""
    out = []
    if gdelt_res is not None:
        new, seen, fails = gdelt_res
        if seen == 0 and fails:
            out.append(f"⚠ Источник новостей GDELT сегодня не отдал ни одной статьи ({fails} запросов из "
                       f"{fails} — отказ, обычно HTTP 429 = GDELT ограничил наш IP). Поток идёт только с "
                       f"NewsAPI.ai: половина мирового скана (мультиязычная) выпала. Если повторяется "
                       f"несколько дней — нужен разбор (пауза между запросами в config/news.yaml).")
    if trends_res is not None:
        n_iot, n_rel, rl, njobs = trends_res
        if njobs and rl >= njobs:
            out.append(f"⚠ Google Trends: все {njobs} ключей сегодня отбиты лимитом (429) — датчик толпы "
                       f"«внимание» сегодня без свежих данных.")
        elif njobs and rl / njobs >= 0.5:
            out.append(f"⚠ Google Trends: отбито лимитом {rl} из {njobs} ключей — обновились только первые "
                       f"в очереди (тематические); ключи реестра «внимания» снова без данных.")
    return out


def _notice(text, path=None):
    """Здоровье источников — СЛУЖЕБНАЯ заметка (владелец 04.11: «что за мусор и зачем?»): в журнал для
    машиниста и пятничного разбора, в Telegram не уходит (бот пропускает silent)."""
    path = pathlib.Path(path) if path else NOTICES
    rec = {"ts": nc.now_utc_iso(), "text": text, "silent": True, "source": "дозор"}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def quality_report(con):
    """Отчёт по полноте автотегов (П1) и дедупу. Возвращает dict для журнала."""
    total = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    uniq = con.execute("SELECT COUNT(*) FROM news WHERE dup_of IS NULL").fetchone()[0]
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    today_total = con.execute(
        "SELECT COUNT(*) FROM news WHERE substr(published_at,1,10)=?", (today,)).fetchone()[0]
    today_uniq = con.execute(
        "SELECT COUNT(*) FROM news WHERE substr(published_at,1,10)=? AND dup_of IS NULL",
        (today,)).fetchone()[0]

    def pct_filled(col):
        if total == 0:
            return 100.0
        n = con.execute(f"SELECT COUNT(*) FROM news WHERE {col} IS NOT NULL").fetchone()[0]
        return round(100.0 * n / total, 1)

    rep = {
        "ts": nc.now_utc_iso(),
        "news_total": total,
        "news_unique": uniq,
        "news_duplicates": total - uniq,
        "dedup_rate_pct": round(100.0 * (total - uniq) / total, 1) if total else 0.0,
        "today_total": today_total,
        "today_unique": today_uniq,
        "tag_fill_pct": {
            "lang": pct_filled("lang"),
            "country": pct_filled("country"),
            "source_type": pct_filled("source_type"),
            "published_at": pct_filled("published_at"),
        },
        "by_source": dict(con.execute("SELECT source, COUNT(*) FROM news GROUP BY source").fetchall()),
        "by_type": dict(con.execute("SELECT source_type, COUNT(*) FROM news GROUP BY source_type").fetchall()),
        "by_lang_top": dict(con.execute(
            "SELECT lang, COUNT(*) c FROM news GROUP BY lang ORDER BY c DESC LIMIT 8").fetchall()),
        "by_country_top": dict(con.execute(
            "SELECT country, COUNT(*) c FROM news WHERE country IS NOT NULL "
            "GROUP BY country ORDER BY c DESC LIMIT 8").fetchall()),
        "trends_points": con.execute("SELECT COUNT(*) FROM trends").fetchone()[0],
    }
    return rep


def print_report(rep):
    print("\n=== Отчёт суточного сборщика (П1) ===")
    print(f"Новостей всего: {rep['news_total']}  | уникальных: {rep['news_unique']}  | "
          f"дублей: {rep['news_duplicates']} ({rep['dedup_rate_pct']}%)")
    print(f"За сегодня (UTC): {rep['today_total']} (уникальных {rep['today_unique']})")
    f = rep["tag_fill_pct"]
    print(f"Полнота автотегов: язык {f['lang']}% | страна {f['country']}% | "
          f"тип {f['source_type']}% | время {f['published_at']}%")
    print(f"По источникам: {rep['by_source']}")
    print(f"По типу:       {rep['by_type']}")
    print(f"По языку:      {rep['by_lang_top']}")
    print(f"По стране:     {rep['by_country_top']}")
    print(f"Google Trends точек ряда: {rep['trends_points']}")


def write_log(rep):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rep, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-trends", action="store_true", help="пропустить Google Trends")
    ap.add_argument("--no-newsapi", action="store_true", help="пропустить NewsAPI.ai")
    ap.add_argument("--no-gdelt", action="store_true", help="пропустить GDELT")
    ap.add_argument("--report", action="store_true", help="только отчёт по текущей базе")
    args = ap.parse_args()

    con = nc.db_connect()

    if args.report:
        rep = quality_report(con)
        print_report(rep)
        return 0

    print(f"=== Суточный сбор новостей {nc.now_utc_iso()} ===")
    news_fail = 0
    gdelt_res = trends_res = None

    # 1. GDELT (мультиязычная основа)
    if not args.no_gdelt:
        try:
            new, seen, fails = gdelt.run_all(con)
            gdelt_res = (new, seen, fails)
            print(f"GDELT: +{new} новых / {seen} получено, сбоев {fails}")
            if new == 0 and fails:
                news_fail += 1
        except Exception as e:  # noqa: BLE001
            news_fail += 1
            print(f"❌ GDELT упал: {e}", file=sys.stderr)

    # 2. NewsAPI.ai (тегированный поток)
    if not args.no_newsapi:
        api_key = os.environ.get("NEWSAPI_AI_KEY")
        if not api_key:
            print("⚠ NEWSAPI_AI_KEY не задан — NewsAPI.ai пропущен", file=sys.stderr)
        else:
            try:
                new, seen, fails = newsapi_ai.run_all(con, api_key)
                print(f"NewsAPI.ai: +{new} новых / {seen} получено, сбоев {fails}")
            except Exception as e:  # noqa: BLE001
                news_fail += 1
                print(f"❌ NewsAPI.ai упал: {e}", file=sys.stderr)

    # 3. Дедуп near-dup по последним суткам (точный дедуп уже на вставке по id)
    marked = nc.dedupe_recent(con, days_back=2)
    print(f"Дедуп: помечено near-дублей за последние сутки: {marked}")

    # 4. Google Trends — best-effort, 429 не валит пайплайн
    if not args.no_trends:
        try:
            import trends
            # verbose=True (31.10): построчно «✅/⏳ trends 'ключ'» в cron.log — иначе 429 по ключам реестра
            # «внимания» был невидим 3,5 месяца (в базе не обновился ни один ключ реестра после 15.07).
            n_iot, n_rel, rl, njobs = trends.run_all(con, verbose=True)
            trends_res = (n_iot, n_rel, rl, njobs)
            if n_iot:
                print(f"Google Trends: {n_iot} точек ряда, {n_rel} related; отбито лимитом 429: {rl}/{njobs} ключей")
            else:
                print(f"Google Trends: данных нет (rate-limited {rl}/{njobs}) — коннектор исправен, "
                      f"Google ограничил IP; не блокирует поток")
        except Exception as e:  # noqa: BLE001
            print(f"⚠ Google Trends пропущен: {e}", file=sys.stderr)

    # 5. Отчёт качества
    rep = quality_report(con)
    print_report(rep)
    write_log(rep)

    # Gate Недели 2: поток должен нормализоваться и тегироваться без ручной правки.
    f = rep["tag_fill_pct"]
    auto_ok = (rep["news_total"] > 0 and f["lang"] >= 95 and f["source_type"] >= 99
               and f["published_at"] >= 95)
    print("\nGate Нед.2:", "✅ автотегирование и дедуп работают" if auto_ok
          else "❌ тегирование неполно — проверь карты нормализации")
    # Здоровье источников → заметки владельцу (31.10). Прежнее выражение кода выхода было тавтологией
    # (при auto_ok всегда 0 независимо от news_fail) — частичный отказ источника никуда не сообщался.
    # Код выхода оставляем 0 при живом потоке: cron_alert трактует ненулевой как «прогон упал, идей не
    # будет», что для деградации одного источника — ложь (П8). Канал — notices.jsonl, текст готов здесь.
    for text in source_health_notices(gdelt_res, trends_res):
        try:
            _notice(text)
        except OSError as e:
            print(f"⚠ заметка о здоровье источников не записана: {e}", file=sys.stderr)
    return 0 if auto_ok else 1


if __name__ == "__main__":
    sys.exit(main())
