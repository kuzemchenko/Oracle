#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/trends.py — коннектор Google Trends через pytrends (MASTER_SPEC §30 п.1:
«pytrends (Google Trends, бесплатно)»; §4 «скрейпер трендов»).

Тянет interest_over_time (ряд интереса 0–100) и related_queries (top/rising) по ключевым
словам тем из config/news.yaml и кладёт в storage/oracle.db (таблицы trends, trends_related).
Идемпотентен: INSERT OR REPLACE по (keyword, geo, date) и (keyword, geo, rank_kind, query).

ВАЖНО про надёжность. Google жёстко лимитирует неофициальный endpoint (HTTP 429), особенно
с датацентровых IP — это внешнее ограничение инфраструктуры, не дефект коннектора. Поэтому:
  • встроен экспоненциальный backoff и пауза между ключевыми словами;
  • pytrends 4.9.2 несовместим с urllib3 2.x (Retry.method_whitelist удалён) — поэтому мы НЕ
    передаём retries в TrendReq, а делаем backoff сами;
  • при устойчивом 429 коннектор НЕ падает: пишет, что получил, помечает rate-limit и выходит 0.
В суточном пайплайне (data/news_ingest.py) тренды — best-effort и не блокируют новостной поток.

Запуск:
    .venv/bin/python data/trends.py                  # все ключи тем из news.yaml
    .venv/bin/python data/trends.py --keyword "brent oil" --timeframe "today 3-m"
    .venv/bin/python data/trends.py --status
"""
import sys
import time
import argparse
import warnings
import pathlib

warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import news_common as nc

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # для mathlib.attention (канонический timeframe П1)
NEWS_CFG = ROOT / "config" / "news.yaml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# Локальная копия канонического окна (П1-гейт): mathlib сюда НЕ импортируем на уровне модуля
# (numpy не нужен суточному фетчу) — _warn_if_not_canonical лениво сверяет её с
# mathlib.attention.TRENDS_TIMEFRAME и громко ругается при любом расхождении.
CANON_TIMEFRAME = "today 3-m"


class RateLimited(Exception):
    pass


def _trendreq():
    from pytrends.request import TrendReq
    # без retries/backoff_factor — иначе pytrends соберёт urllib3.Retry(method_whitelist=...),
    # которого нет в urllib3 2.x; backoff делаем сами в _call.
    return TrendReq(
        hl="en-US", tz=0, timeout=(10, 25),
        requests_args={"headers": {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}},
    )


def _call(fn, *a, tries=4, pause=8.0, **kw):
    """Вызвать метод pytrends с backoff. RateLimited — ТОЛЬКО при реальном устойчивом 429;
    прочие устойчивые сбои (смена API, KeyError, таймауты) пробрасываются КАК ЕСТЬ — кросс-ревью
    №4 (HIGH): маскировка поломки источника под «внешний лимит 429» — нечестная диагностика."""
    from pytrends.exceptions import TooManyRequestsError
    last = None
    for i in range(tries):
        try:
            return fn(*a, **kw)
        except TooManyRequestsError as e:
            last = e
            time.sleep(pause * (i + 1))
        except Exception as e:  # noqa: BLE001 — сетевые сбои тоже под backoff
            last = e
            time.sleep(pause * (i + 1))
    if isinstance(last, TooManyRequestsError):
        raise RateLimited(str(last))
    raise last


def fetch_keyword(keyword, geo="", timeframe=CANON_TIMEFRAME):
    """Вернуть (rows_iot, rows_related). При 429 поднимает RateLimited."""
    pt = _trendreq()
    _call(pt.build_payload, [keyword], timeframe=timeframe, geo=geo)
    iot = _call(pt.interest_over_time)
    rows_iot = []
    if iot is not None and not iot.empty:
        partial = iot["isPartial"] if "isPartial" in iot.columns else None
        for idx, val in iot[keyword].items():
            date = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
            is_partial = bool(partial.loc[idx]) if partial is not None else False
            rows_iot.append((keyword, geo, date, int(val), 1 if is_partial else 0))
    rows_related = []
    try:
        rq = _call(pt.related_queries)
        rows_related = _related_rows(rq, keyword, geo)
    except Exception:  # noqa: BLE001 — related строго best-effort: ряд интереса важнее (кросс-ревью №3)
        pass
    return rows_iot, rows_related


def _related_value(v):
    """Значение related-запроса → int|None. Google для rising отдаёт и НЕчисловое 'Breakout'
    (рост >5000%) — раньше int() ронял ВЕСЬ fetch_keyword и терял уже полученный ряд интереса
    (кросс-ревью №3, HIGH). Нечисловое → None (честное «величина не числом»)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _related_rows(rq, keyword, geo):
    """Разбор related_queries в строки БД (вынесен для тестируемости без сети)."""
    out = []
    block = (rq or {}).get(keyword) or {}
    for kind in ("top", "rising"):
        df = block.get(kind)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                out.append((keyword, geo, kind, str(r["query"]), _related_value(r["value"])))
    return out


def store(con, rows_iot, rows_related, timeframe=CANON_TIMEFRAME):
    now = nc.now_utc_iso()
    if rows_iot:
        # timeframe пишется В КАЖДУЮ строку (кросс-ревью П1-гейта, BLOCKER): нормировки разных
        # окон несравнимы, rows_for_attention фильтрует по каноническому окну. NB: PRIMARY KEY
        # (keyword,geo,date) — неканонический фетч ЗАТИРАЕТ канонические строки тех же дат;
        # фильтр честно вернёт «мало истории»/пусто, а не смесь нормировок.
        con.executemany(
            "INSERT OR REPLACE INTO trends (keyword,geo,date,interest,is_partial,source,fetched_at,timeframe)"
            " VALUES (?,?,?,?,?, 'google_trends', ?, ?)",
            [(*r, now, timeframe) for r in rows_iot],
        )
    if rows_related:
        con.executemany(
            "INSERT OR REPLACE INTO trends_related (keyword,geo,rank_kind,query,value,fetched_at)"
            " VALUES (?,?,?,?,?, ?)",
            [(*r, now) for r in rows_related],
        )
    con.commit()
    return len(rows_iot), len(rows_related)


def load_keywords():
    import yaml
    with open(NEWS_CFG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    tr = cfg.get("trends", {})
    timeframe = tr.get("timeframe", CANON_TIMEFRAME)
    geos = tr.get("geos", [""])
    pause = float(tr.get("pause_sec", 10.0))
    kws = []
    for theme in cfg.get("themes", []):
        for kw in theme.get("trends_keywords", []):
            kws.append(kw)
    # явный список поверх тем
    kws += tr.get("extra_keywords", [])
    return kws, timeframe, geos, pause


def run_all(con, verbose=True):
    kws, timeframe, geos, pause = load_keywords()
    _warn_if_not_canonical(timeframe)
    jobs = [(kw, geo) for kw in kws for geo in geos]
    n_iot = n_rel = 0
    rate_limited = 0
    for i, (kw, geo) in enumerate(jobs):
        try:
            ri, rr = fetch_keyword(kw, geo=geo, timeframe=timeframe)
            a, b = store(con, ri, rr, timeframe=timeframe)
            n_iot += a
            n_rel += b
            if verbose:
                print(f"✅ trends '{kw}' geo='{geo or 'world'}': {a} точек ряда, {b} related")
        except RateLimited as e:
            rate_limited += 1
            if verbose:
                print(f"⏳ trends '{kw}' geo='{geo or 'world'}': Google 429 (внешний лимит IP) — пропуск")
        except Exception as e:  # noqa: BLE001
            print(f"❌ trends '{kw}': {e}", file=sys.stderr)
        if i < len(jobs) - 1:
            time.sleep(pause)
    return n_iot, n_rel, rate_limited, len(jobs)


def rows_for_attention(con, keyword, geo="", timeframe=None):
    """Строки ряда для mathlib.attention (П1-гейт 04.07): ТОЛЬКО каноническое окно timeframe —
    без подмены шкалы неканоническим фетчем (кросс-ревью, BLOCKER). Выбор ПОСЛЕДНЕГО фетча здесь
    НЕ делается: SQL MAX(fetched_at) — лексикографический и ломается на смеси смещений (кросс-
    ревью №4, HIGH); хронологический выбор — ЕДИНСТВЕННЫМ местом в attention_from_rows.
    Возвращает [(date, interest, is_partial, fetched_at), ...] по возрастанию даты."""
    if timeframe is None:
        from mathlib import attention as A
        timeframe = A.TRENDS_TIMEFRAME
    return con.execute(
        "SELECT date, interest, is_partial, fetched_at FROM trends"
        " WHERE keyword=? AND geo=? AND timeframe=?"
        " ORDER BY date ASC", (keyword, geo, timeframe)).fetchall()


def _warn_if_not_canonical(timeframe):
    """П1-гейт 04.07: timeframe — канонический параметр датчика (scores сравнимы только внутри
    одного окна; пороги LEVEL_*/MOM_* валидны для канона). Фетч с другим окном — громкая пометка."""
    from mathlib import attention as A
    if CANON_TIMEFRAME != A.TRENDS_TIMEFRAME:
        print(f"⚠ рассинхрон констант: trends.CANON_TIMEFRAME '{CANON_TIMEFRAME}' ≠ "
              f"attention.TRENDS_TIMEFRAME '{A.TRENDS_TIMEFRAME}' — почини одну из них")
    if timeframe != A.TRENDS_TIMEFRAME:
        print(f"⚠ timeframe '{timeframe}' ≠ канонического '{A.TRENDS_TIMEFRAME}' (П1-гейт): "
              f"scores датчика внимания будут НЕсравнимы с боевыми, пороги не валидны")


def status(con):
    n = con.execute("SELECT COUNT(*) FROM trends").fetchone()[0]
    nk = con.execute("SELECT COUNT(DISTINCT keyword) FROM trends").fetchone()[0]
    nr = con.execute("SELECT COUNT(*) FROM trends_related").fetchone()[0]
    print(f"trends: {n} точек ряда по {nk} ключам; trends_related: {nr} строк")
    for kw, c, mn, mx in con.execute(
            "SELECT keyword, COUNT(*), MIN(date), MAX(date) FROM trends GROUP BY keyword"):
        print(f"  {kw:20} {c:4d} точек  {mn}..{mx}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword")
    ap.add_argument("--geo", default="")
    ap.add_argument("--timeframe", default=CANON_TIMEFRAME)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    con = nc.db_connect()
    if args.status:
        status(con)
        return 0

    if args.keyword:
        _warn_if_not_canonical(args.timeframe)
        try:
            ri, rr = fetch_keyword(args.keyword, geo=args.geo, timeframe=args.timeframe)
            a, b = store(con, ri, rr, timeframe=args.timeframe)
            print(f"✅ trends '{args.keyword}': {a} точек ряда, {b} related")
        except RateLimited as e:
            print(f"⏳ trends '{args.keyword}': Google 429 (внешний лимит IP, не дефект). {e}")
        status(con)
        return 0

    n_iot, n_rel, rl, njobs = run_all(con)
    print(f"\ntrends итог: {n_iot} точек ряда, {n_rel} related; rate-limited {rl}/{njobs} запросов")
    if rl == njobs and njobs:
        print("⏳ Google ограничил все запросы (429) — типично для датацентровых IP; "
              "коннектор исправен, данные подтянутся с разрешённого IP/позже.")
    status(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
