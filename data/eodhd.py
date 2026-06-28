#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/eodhd.py — коннектор котировок EODHD (MASTER_SPEC §4 блок A «Котировки», §30 п.1).

Тянет дневные EOD-ряды для всех core_symbols из config/universe.yaml в storage/oracle.db
(таблица quotes). Идемпотентен: повторный запуск дозаливает только новые/недостающие даты
(INSERT OR REPLACE по (symbol, date)). Источник цены для тайминга, сверки прогнозов §9,
бенчмарка §30 и калибровки §23. Сам по себе детерминирован — никаких LLM.

Запуск:
    set -a && . ./.env && set +a
    python3 data/eodhd.py                 # дозалить все core_symbols
    python3 data/eodhd.py --full          # перекачать всю историю с history_from
    python3 data/eodhd.py --symbol SPY.US # один символ
    python3 data/eodhd.py --status        # что лежит в базе
"""
import os, sys, json, sqlite3, argparse, datetime, urllib.request, urllib.error, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "oracle.db"
UNIVERSE = ROOT / "config" / "universe.yaml"
API = "https://eodhd.com/api/eod/{symbol}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    symbol          TEXT NOT NULL,
    date            TEXT NOT NULL,          -- ISO YYYY-MM-DD
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    adjusted_close  REAL,
    volume          INTEGER,
    source          TEXT DEFAULT 'eodhd',
    fetched_at      TEXT,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_quotes_symbol ON quotes(symbol);
CREATE INDEX IF NOT EXISTS idx_quotes_date   ON quotes(date);

-- Фундаментал (EODHD /fundamentals): структура владения/оценка для поведенческого,
-- фундаментального и анти-манип. агентов. Храним извлечённые скаляры + полный JSON.
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol               TEXT PRIMARY KEY,
    name                 TEXT,
    sector               TEXT,
    industry             TEXT,
    market_cap_mln       REAL,
    pe_ratio             REAL,
    forward_pe           REAL,
    eps                  REAL,
    shares_outstanding   REAL,
    shares_float         REAL,
    pct_insiders         REAL,
    pct_institutions     REAL,
    shares_short         REAL,        -- на нашем плане часто NULL ("нет данных", П8)
    short_pct_float      REAL,        -- на нашем плане часто NULL
    raw_json             TEXT,        -- полный ответ (ничего не теряем)
    fetched_at           TEXT
);

-- Инсайдерские сделки (EODHD /insider-transactions): детектор «кто продаёт нам» (§4, §14).
CREATE TABLE IF NOT EXISTS insider_tx (
    id                   TEXT PRIMARY KEY,   -- sha1(symbol|tx_date|owner|code|amount)
    symbol               TEXT NOT NULL,
    tx_date              TEXT,
    report_date          TEXT,
    owner_name           TEXT,
    owner_title          TEXT,
    code                 TEXT,               -- P=покупка, S=продажа, ...
    amount               REAL,
    price                REAL,
    acquired_disposed    TEXT,               -- A=acquired, D=disposed
    post_amount          REAL,
    link                 TEXT,
    fetched_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_insider_symbol ON insider_tx(symbol);

-- Календарь отчётов (EODHD /calendar/earnings): тайминг + cui bono (совпадение с отчётами).
CREATE TABLE IF NOT EXISTS earnings_calendar (
    symbol               TEXT NOT NULL,
    report_date          TEXT NOT NULL,      -- дата выхода отчёта
    period_date          TEXT,               -- отчётный период
    before_after_market  TEXT,
    actual               REAL,
    estimate             REAL,
    fetched_at           TEXT,
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX IF NOT EXISTS idx_earn_symbol ON earnings_calendar(symbol);

-- Свёртка опционной цепочки (EODHD Unicorn Bay, marketplace-аддон): ATM IV, skew, put/call OI/vol,
-- term-structure. Тайминг (IV), антиманипуляция (OI), риск (ликвидность хеджа). Считает mathlib.options.
CREATE TABLE IF NOT EXISTS options_summary (
    symbol      TEXT PRIMARY KEY,
    asof        TEXT,            -- дата спота, по которому считалась ATM/moneyness
    summary     TEXT,            -- JSON метрик (mathlib.options.summarize)
    fetched_at  TEXT
);
"""


def load_universe():
    import yaml
    with open(UNIVERSE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def db_connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def fetch_eod(symbol, api_key, date_from, date_to):
    url = (API.format(symbol=symbol)
           + f"?api_token={api_key}&fmt=json&from={date_from}&to={date_to}")
    req = urllib.request.Request(url, headers={"User-Agent": "oracle/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} для {symbol}") from e
    if not body.strip().startswith("["):
        raise RuntimeError(f"{symbol}: неожиданный ответ EODHD: {body[:120]!r}")
    return json.loads(body)


def last_date(con, symbol):
    row = con.execute("SELECT MAX(date) FROM quotes WHERE symbol=?", (symbol,)).fetchone()
    return row[0] if row and row[0] else None


def upsert(con, symbol, rows):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    con.executemany(
        """INSERT OR REPLACE INTO quotes
           (symbol,date,open,high,low,close,adjusted_close,volume,source,fetched_at)
           VALUES (?,?,?,?,?,?,?,?, 'eodhd', ?)""",
        [(symbol, r["date"], r.get("open"), r.get("high"), r.get("low"),
          r.get("close"), r.get("adjusted_close"), r.get("volume"), now) for r in rows],
    )
    con.commit()
    return len(rows)


def sync_symbol(con, symbol, api_key, history_from, full=False):
    today = datetime.date.today().isoformat()
    start = history_from
    if not full:
        ld = last_date(con, symbol)
        if ld:
            start = (datetime.date.fromisoformat(ld) + datetime.timedelta(days=1)).isoformat()
    if start > today:
        return 0, "актуально"
    rows = fetch_eod(symbol, api_key, start, today)
    n = upsert(con, symbol, rows) if rows else 0
    return n, f"{start}..{today}"


def ensure_history(con, symbols, api_key, *, min_bars=60, history_from="2019-01-01"):
    """Динамический добор истории (B2.6): для тикеров без достаточной локальной истории — тянем EOD
    из EODHD на лету и вставляем в quotes. Снимает «универсум как стенку»: любой ликвидный тикер
    (напр. предложенный картографом из новостей) становится анализируемым ПО СУЩЕСТВУ, а не по
    членству в списке. Уже скачанные — пропускаем (кэш растёт сам). Возвращает {fetched, had, failed}."""
    def _bars(sym):
        return con.execute("SELECT COUNT(*) FROM quotes WHERE symbol=? AND close IS NOT NULL",
                           (sym,)).fetchone()[0]
    fetched, had, failed, refreshed = [], [], [], []
    for sym in dict.fromkeys(s for s in (symbols or []) if s):
        if _bars(sym) >= min_bars:
            # БАЗА ЕСТЬ, но цены могли замёрзнуть (крон синкает лишь core): ДОсинкиваем инкрементально
            # до сегодня — иначе шок корня берётся из устаревших цен и к событию не относится. Дёшево:
            # sync_symbol(full=False) от last_date+1; если уже актуально — без обращения к API.
            try:
                n, _ = sync_symbol(con, sym, api_key, history_from, full=False)
                if n:
                    refreshed.append(sym)
            except Exception as e:  # noqa: BLE001 — досинк best-effort, база уже пригодна
                failed.append({"symbol": sym, "почему": f"досинк не удался: {str(e)[:60]}"})
            had.append(sym)
            continue
        try:
            sync_symbol(con, sym, api_key, history_from, full=True)
        except Exception as e:  # noqa: BLE001
            failed.append({"symbol": sym, "почему": str(e)[:80]})
            continue
        if _bars(sym) >= min_bars:
            fetched.append(sym)
        else:
            failed.append({"symbol": sym, "почему": f"после фетча < {min_bars} баров"})
    return {"fetched": fetched, "had": had, "refreshed": refreshed, "failed": failed}


# ── EODHD: фундаментал / инсайдеры / календарь (всё в нашей подписке All-in-One) ──────
import hashlib  # noqa: E402

EODHD = "https://eodhd.com/api"


def _get_json(url, api_key, expect=("[", "{")):
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}api_token={api_key}&fmt=json"
    req = urllib.request.Request(full, headers={"User-Agent": "oracle/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    s = body.strip()
    if expect and s[:1] not in expect:
        raise RuntimeError(f"неожиданный ответ EODHD: {s[:120]!r}")
    return json.loads(body) if s else None


def _f(d, *path):
    """Безопасный float по вложенному пути; None → None (П8: не выдумываем 0)."""
    x = d
    for k in path:
        x = x.get(k) if isinstance(x, dict) else None
    try:
        return float(x) if x not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


def fetch_fundamentals(symbol, api_key):
    return _get_json(f"{EODHD}/fundamentals/{symbol}", api_key, expect=("{",))


def sync_fundamentals(con, symbol, api_key):
    f = fetch_fundamentals(symbol, api_key)
    if not isinstance(f, dict) or not f:
        return 0
    g = f.get("General") or {}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    con.execute(
        """INSERT OR REPLACE INTO fundamentals
           (symbol,name,sector,industry,market_cap_mln,pe_ratio,forward_pe,eps,
            shares_outstanding,shares_float,pct_insiders,pct_institutions,
            shares_short,short_pct_float,raw_json,fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (symbol, g.get("Name"), g.get("Sector"), g.get("Industry"),
         _f(f, "Highlights", "MarketCapitalizationMln"), _f(f, "Highlights", "PERatio"),
         _f(f, "Valuation", "ForwardPE"), _f(f, "Highlights", "EarningsShare"),
         _f(f, "SharesStats", "SharesOutstanding"), _f(f, "SharesStats", "SharesFloat"),
         _f(f, "SharesStats", "PercentInsiders"), _f(f, "SharesStats", "PercentInstitutions"),
         _f(f, "SharesStats", "SharesShort"), _f(f, "SharesStats", "ShortPercentFloat"),
         json.dumps(f, ensure_ascii=False), now))
    con.commit()
    return 1


def fetch_insider(symbol, api_key, limit=50):
    return _get_json(f"{EODHD}/insider-transactions/?code={symbol}&limit={limit}", api_key)


def sync_insider(con, symbol, api_key, limit=50):
    rows = fetch_insider(symbol, api_key, limit) or []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    n = 0
    for r in rows:
        txd = r.get("transactionDate") or r.get("date")
        owner = r.get("ownerName") or ""
        code = r.get("transactionCode") or ""
        amt = r.get("transactionAmount")
        rid = hashlib.sha1(f"{symbol}|{txd}|{owner}|{code}|{amt}".encode("utf-8")).hexdigest()
        con.execute(
            """INSERT OR REPLACE INTO insider_tx
               (id,symbol,tx_date,report_date,owner_name,owner_title,code,amount,price,
                acquired_disposed,post_amount,link,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, symbol, txd, r.get("reportDate"), owner, r.get("ownerTitle"), code,
             amt, r.get("transactionPrice"), r.get("transactionAcquiredDisposed"),
             r.get("postTransactionAmount"), r.get("link"), now))
        n += 1
    con.commit()
    return n


def fetch_earnings_calendar(symbols, api_key, date_from, date_to):
    syms = ",".join(symbols)
    data = _get_json(f"{EODHD}/calendar/earnings?symbols={syms}&from={date_from}&to={date_to}",
                     api_key, expect=("{", "["))
    if isinstance(data, dict):
        return data.get("earnings") or []
    return data or []


def sync_earnings(con, symbols, api_key, horizon_days=120):
    today = datetime.date.today()
    rows = fetch_earnings_calendar(symbols, api_key, today.isoformat(),
                                   (today + datetime.timedelta(days=horizon_days)).isoformat())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    n = 0
    for r in rows:
        sym = r.get("code")
        rd = r.get("report_date")
        if not sym or not rd:
            continue
        con.execute(
            """INSERT OR REPLACE INTO earnings_calendar
               (symbol,report_date,period_date,before_after_market,actual,estimate,fetched_at)
               VALUES (?,?,?,?,?,?,?)""",
            (sym, rd, r.get("date"), r.get("before_after_market"),
             r.get("actual"), r.get("estimate"), now))
        n += 1
    con.commit()
    return n


def fetch_options(symbol, api_key, limit=1000):
    """Опционная цепочка EODHD Unicorn Bay (marketplace-аддон). Тикер БЕЗ суффикса .US."""
    import urllib.parse
    base = symbol.split(".")[0]
    f = urllib.parse.quote("filter[underlying_symbol]")
    url = (f"{EODHD}/mp/unicornbay/options/eod?{f}={base}&sort=-exp_date&limit={limit}")
    data = _get_json(url, api_key, expect=("{",))
    rows = (data or {}).get("data") or []
    # JSON:API-стиль ({attributes}) ИЛИ плоские dict — нормализуем к плоским
    return [(r.get("attributes") if isinstance(r.get("attributes"), dict) else r) for r in rows]


def sync_options(con, symbol, api_key):
    """Фетч цепочки → свёртка mathlib.options (спот из quotes) → options_summary."""
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from mathlib import options as OPT
    contracts = fetch_options(symbol, api_key)
    spot_row = con.execute("SELECT close, date FROM quotes WHERE symbol=? ORDER BY date DESC LIMIT 1",
                           (symbol,)).fetchone()
    spot = spot_row[0] if spot_row else None
    asof = spot_row[1] if spot_row else None
    summary = OPT.summarize(contracts, spot=spot)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    con.execute("INSERT OR REPLACE INTO options_summary (symbol,asof,summary,fetched_at) VALUES (?,?,?,?)",
                (symbol, asof, json.dumps(summary, ensure_ascii=False), now))
    con.commit()
    return summary


def cmd_status(con):
    cur = con.execute(
        "SELECT symbol, COUNT(*), MIN(date), MAX(date) FROM quotes GROUP BY symbol ORDER BY symbol")
    print(f"{'symbol':12} {'rows':>6}  {'from':10} .. {'to':10}")
    total = 0
    for sym, n, mn, mx in cur.fetchall():
        total += n
        print(f"{sym:12} {n:6d}  {mn} .. {mx}")
    print(f"итого строк: {total}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="перекачать всю историю с history_from")
    ap.add_argument("--symbol", help="только один символ")
    ap.add_argument("--status", action="store_true", help="показать содержимое базы")
    ap.add_argument("--extras", action="store_true",
                    help="подтянуть фундаментал/инсайдеров/календарь (Tier 0 EODHD) для символов")
    ap.add_argument("--options", action="store_true",
                    help="подтянуть свёртку опционов (Unicorn Bay marketplace-аддон) для символов")
    ap.add_argument("--also", default="",
                    help="доп. символы через запятую (помимо core_symbols), напр. SPCX.US,RKLB.US")
    args = ap.parse_args()

    con = db_connect()
    if args.status:
        cmd_status(con)
        return 0

    api_key = os.environ.get("EODHD_API_KEY")
    if not api_key:
        print("ОШИБКА: EODHD_API_KEY не задан (source .env)", file=sys.stderr)
        return 1

    uni = load_universe()
    history_from = uni.get("history_from", "2015-01-01")
    also = [s.strip() for s in args.also.split(",") if s.strip()]
    symbols = [args.symbol] if args.symbol else (uni["core_symbols"] + also)

    failures = 0
    for sym in symbols:
        try:
            n, span = sync_symbol(con, sym, api_key, history_from, full=args.full)
            print(f"✅ {sym:12} +{n:5d} строк  ({span})")
        except Exception as e:
            failures += 1
            print(f"❌ {sym:12} {e}", file=sys.stderr)

    if args.extras:
        print("\n— Tier 0 EODHD: фундаментал / инсайдеры / календарь —")
        try:
            ne = sync_earnings(con, symbols, api_key)
            print(f"📅 календарь отчётов: +{ne} записей по {len(symbols)} символам")
        except Exception as e:
            failures += 1
            print(f"❌ календарь: {e}", file=sys.stderr)
        for sym in symbols:
            try:
                nf = sync_fundamentals(con, sym, api_key)
                ni = sync_insider(con, sym, api_key)
                row = con.execute("SELECT shares_float, pct_insiders, pct_institutions, market_cap_mln "
                                  "FROM fundamentals WHERE symbol=?", (sym,)).fetchone()
                fl = f"float={row[0]:,.0f} инсайд={row[1]}% инст={row[2]}% mcap=${row[3]:,.0f}млн" if row else "—"
                print(f"📊 {sym:12} fund={nf} инсайд_сделок={ni} · {fl}")
            except Exception as e:
                failures += 1
                print(f"❌ {sym:12} extras: {e}", file=sys.stderr)

    if args.options:
        print("\n— Опционы (Unicorn Bay): ATM IV / skew / put-call OI —")
        for sym in symbols:
            try:
                s = sync_options(con, sym, api_key)
                if s.get("insufficient"):
                    print(f"🟦 {sym:12} опционов нет/недостаточно")
                else:
                    print(f"📈 {sym:12} ATM_IV={s['atm_iv']} skew={s['iv_skew_25d_put_minus_call']} "
                          f"put/call_OI={s['put_call_oi_ratio']} OI={s['total_open_interest']} "
                          f"ликвид={s['liquid']}")
            except Exception as e:
                failures += 1
                print(f"❌ {sym:12} опционы: {e}", file=sys.stderr)

    print()
    cmd_status(con)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
