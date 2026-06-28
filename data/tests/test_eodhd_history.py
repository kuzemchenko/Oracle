# -*- coding: utf-8 -*-
"""Тесты динамического добора истории (data/eodhd.ensure_history, B2.6).

Снимает «универсум как стенку»: тикер без локальной истории дотягивается из EODHD на лету.
Сеть мокается (monkeypatch fetch_eod) — тест гермётичный.
"""
import datetime
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from data import eodhd as E    # noqa: E402


def _rows(n):
    base = datetime.date(2020, 1, 1)
    return [{"date": (base + datetime.timedelta(days=i)).isoformat(),
             "open": 100, "high": 101, "low": 99, "close": 100 + i * 0.1,
             "adjusted_close": 100 + i * 0.1, "volume": 1_000_000} for i in range(n)]


def test_ensure_history_fetches_missing(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.executescript(E.SCHEMA)
    E.upsert(con, "OLD.US", _rows(70))                       # уже есть достаточная история
    monkeypatch.setattr(E, "fetch_eod", lambda sym, key, f, t: _rows(80))  # сеть → синтетика
    res = E.ensure_history(con, ["OLD.US", "NEW.US", "NEW.US"], "key", min_bars=60)
    assert res["had"] == ["OLD.US"]                          # было — не тянем
    assert res["fetched"] == ["NEW.US"]                      # дотянули (дедуп: один раз)
    assert res["failed"] == []
    assert con.execute("SELECT COUNT(*) FROM quotes WHERE symbol='NEW.US'").fetchone()[0] == 80


def test_ensure_history_reports_failure(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.executescript(E.SCHEMA)

    def boom(sym, key, f, t):
        raise RuntimeError("HTTP 404 для GHOST.US")

    monkeypatch.setattr(E, "fetch_eod", boom)
    res = E.ensure_history(con, ["GHOST.US"], "key", min_bars=60)
    assert res["fetched"] == [] and res["had"] == []
    assert res["failed"][0]["symbol"] == "GHOST.US"          # честно отмечен провал (П8), не падаем
