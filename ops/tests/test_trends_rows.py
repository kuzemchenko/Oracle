# -*- coding: utf-8 -*-
"""Тесты загрузчика rows_for_attention и миграции схемы trends (П1-гейт 04.07, stage-review L-7/B-1)."""
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

from data import trends as T           # noqa: E402
import news_common as nc               # noqa: E402
from mathlib import attention as A     # noqa: E402


def _mem_db():
    con = sqlite3.connect(":memory:")
    con.executescript(nc.SCHEMA)
    return con


def _ins(con, kw, date, interest, fetched_at, timeframe, partial=0):
    con.execute("INSERT OR REPLACE INTO trends (keyword,geo,date,interest,is_partial,source,fetched_at,timeframe)"
                " VALUES (?,?,?,?,?,'google_trends',?,?)", (kw, "", date, interest, partial, fetched_at, timeframe))


def test_rows_for_attention_canonical_only_and_chronological_fetch():
    con = _mem_db()
    # старый фетч канонического окна — с НЕнулевым смещением (кросс-ревью №4: SQL MAX по строке
    # выбрал бы его; хронологический выбор — в attention_from_rows)
    for d in range(1, 5):
        _ins(con, "uranium", f"2026-05-0{d}", 90, "2026-06-05T10:00:00+02:00", A.TRENDS_TIMEFRAME)
    # свежий фетч канонического окна (по времени позже: 09:00Z > 08:00Z, хотя по строке «меньше»)
    for d in range(1, 9):
        _ins(con, "uranium", f"2026-06-0{d}", 10 + d, "2026-06-05T09:00:00+00:00", A.TRENDS_TIMEFRAME)
    # неканонический фетч (кросс-ревью BLOCKER: не должен подменять шкалу) — ещё свежее
    _ins(con, "uranium", "2026-06-09", 50, "2026-06-10T00:00:00+00:00", "today 12-m")
    rows = T.rows_for_attention(con, "uranium")
    assert len(rows) == 12                                   # весь канон (оба фетча), 12-m исключён
    assert all(len(r) == 4 for r in rows)
    # end-to-end: датчик берёт ХРОНОЛОГИЧЕСКИ последний фетч канона
    r = A.attention_from_rows(rows)
    assert r["фетч_utc"] == "2026-06-05T09:00:00+00:00"
    assert r["n"] == 8 and r["последний"] < 30               # ряд именно свежего фетча


def test_rows_for_attention_empty_table_and_missing_keyword():
    con = _mem_db()
    assert T.rows_for_attention(con, "нет такого") == []


def test_store_writes_timeframe_and_migration_adds_column():
    # B-1 stage-review: старая схема (без timeframe) обязана мигрироваться КОДОМ (db_connect._migrate),
    # иначе восстановленная из бэкапа БД тихо роняет суточный фетч.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE trends (keyword TEXT NOT NULL, geo TEXT NOT NULL, date TEXT NOT NULL,"
                " interest INTEGER, is_partial INTEGER DEFAULT 0, source TEXT DEFAULT 'google_trends',"
                " fetched_at TEXT, PRIMARY KEY (keyword, geo, date))")
    nc._migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(trends)")}
    assert "timeframe" in cols
    nc._migrate(con)                                         # идемпотентно
    # store() после миграции работает и пишет timeframe
    T.store(con, [("brent oil", "", "2026-07-01", 42, 0)], [], timeframe=A.TRENDS_TIMEFRAME)
    row = con.execute("SELECT interest, timeframe FROM trends WHERE keyword='brent oil'").fetchone()
    assert row == (42, A.TRENDS_TIMEFRAME)


def test_legacy_rows_get_null_and_are_excluded_from_canon():
    # Кросс-ревью №2 (BLOCKER): каким окном тянули легаси-строки — знать нельзя; миграция даёт им
    # NULL (не «канон по умолчанию»), и канонический расчёт их НЕ использует до перефетча.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE trends (keyword TEXT NOT NULL, geo TEXT NOT NULL, date TEXT NOT NULL,"
                " interest INTEGER, is_partial INTEGER DEFAULT 0, source TEXT DEFAULT 'google_trends',"
                " fetched_at TEXT, PRIMARY KEY (keyword, geo, date))")
    con.execute("INSERT INTO trends (keyword,geo,date,interest,fetched_at)"
                " VALUES ('uranium','','2026-05-01',77,'2026-05-02T00:00:00+00:00')")
    nc._migrate(con)
    assert con.execute("SELECT timeframe FROM trends").fetchone() == (None,)
    assert T.rows_for_attention(con, "uranium") == []        # NULL ≠ канон — честно пусто
    # свежий канонический фетч наполняет канон
    T.store(con, [("uranium", "", "2026-06-01", 55, 0)], [], timeframe=A.TRENDS_TIMEFRAME)
    assert len(T.rows_for_attention(con, "uranium")) == 1


def test_related_breakout_value_does_not_crash():
    # Кросс-ревью №3 (HIGH): rising related с value='Breakout' (нечисловое, Google так отдаёт
    # рост >5000%) не роняет разбор и не теряет остальные строки; нечисловое → None.
    import pandas as pd
    rq = {"uranium": {"top": pd.DataFrame([{"query": "uranium etf", "value": 100}]),
                      "rising": pd.DataFrame([{"query": "uranium squeeze", "value": "Breakout"},
                                              {"query": "uranium price", "value": 250}])}}
    rows = T._related_rows(rq, "uranium", "")
    assert ("uranium", "", "top", "uranium etf", 100) in rows
    assert ("uranium", "", "rising", "uranium squeeze", None) in rows    # честный None, не крэш
    assert ("uranium", "", "rising", "uranium price", 250) in rows


def test_call_does_not_mask_failures_as_429():
    # Кросс-ревью №4 (HIGH): устойчивый НЕ-429 сбой (смена API/KeyError) не маскируется под
    # «внешний лимит 429» — пробрасывается как есть; настоящий 429 → RateLimited.
    import pytest
    from pytrends.exceptions import TooManyRequestsError

    def broken():
        raise KeyError("api changed")

    with pytest.raises(KeyError):
        T._call(broken, tries=2, pause=0)

    def limited():
        raise TooManyRequestsError("429", type("Resp", (), {"status_code": 429})())

    with pytest.raises(T.RateLimited):
        T._call(limited, tries=2, pause=0)


def test_canonical_constants_in_sync():
    # L-5: локальная копия канона в trends.py обязана совпадать с mathlib.attention
    assert T.CANON_TIMEFRAME == A.TRENDS_TIMEFRAME
