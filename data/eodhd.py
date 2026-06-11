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
    symbols = [args.symbol] if args.symbol else uni["core_symbols"]

    failures = 0
    for sym in symbols:
        try:
            n, span = sync_symbol(con, sym, api_key, history_from, full=args.full)
            print(f"✅ {sym:12} +{n:5d} строк  ({span})")
        except Exception as e:
            failures += 1
            print(f"❌ {sym:12} {e}", file=sys.stderr)
    print()
    cmd_status(con)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
