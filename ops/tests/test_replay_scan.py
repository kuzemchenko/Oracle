# -*- coding: utf-8 -*-
"""Тесты replay-драйвера Д1 (ops/replay_scan.py): asof-восстановление среза по fetched_at,
честный отказ при невосстановимом состоянии (П8), сравнение конфигураций на фикстуре.
Герметично: временная БД-файл, боевая БД не трогается, журналы не пишутся."""
import sqlite3
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import replay_scan as RS                      # noqa: E402

T_EARLY = "2026-06-10T06:50:00+00:00"
T_LATE = "2026-07-05T15:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "fixture.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, "
                "close REAL, adjusted_close REAL, volume INTEGER, source TEXT, fetched_at TEXT, "
                "PRIMARY KEY(symbol,date))")
    con.execute("CREATE TABLE trends (keyword TEXT, date TEXT, interest INTEGER)")
    con.execute("CREATE TABLE news (id TEXT PRIMARY KEY, source TEXT, title TEXT, lang TEXT, "
                "published_at TEXT, fetched_at TEXT, dup_of TEXT)")
    # AAA: 40 баров, зафетчены рано; BBB: зафетчен ПОЗЖЕ окна; IDX: индекс — исключается
    for i in range(40):
        d = f"2026-05-{i % 28 + 1:02d}" if i < 28 else f"2026-06-{i - 27:02d}"
        con.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("AAA.US", d, 10, 11, 9, 10 + i * 0.01, 10 + i * 0.01, 1000 + i, "t", T_EARLY))
        con.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("BBB.US", d, 10, 11, 9, 10, 10, 500, "t", T_LATE))
        con.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("XXX.INDX", d, 10, 11, 9, 10, 10, 0, "t", T_EARLY))
    for i in range(12):
        con.execute("INSERT INTO trends VALUES (?,?,?)", ("kw", f"2026-06-{i + 5:02d}", 10 + i))
    con.execute("INSERT INTO news VALUES ('1','s','заголовок один','en',"
                "'2026-06-14T08:00:00+00:00','2026-06-14T08:30:00+00:00',NULL)")
    con.commit()
    con.close()
    return p


def test_universe_asof_respects_fetched_at_and_indx(db):
    con = RS._connect_ro(db)
    try:
        assert RS.universe_asof(con, "2026-06-15T09:00:00+00:00") == ["AAA.US"]   # BBB позже, INDX вне
        assert set(RS.universe_asof(con, "2026-07-06T09:00:00+00:00")) == {"AAA.US", "BBB.US"}
        assert RS.universe_asof(con, "2026-01-01T09:00:00+00:00") == []
    finally:
        con.close()


def test_quotes_asof_slices_by_fetched_at(db):
    con = RS._connect_ro(db)
    try:
        q = RS.quotes_asof(con, "BBB.US", "2026-06-15T09:00:00+00:00")
        assert q == []                                     # ещё не был в БД
        q = RS.quotes_asof(con, "AAA.US", "2026-06-15T09:00:00+00:00")
        assert len(q) == 40 and q[0]["date"] < q[-1]["date"]   # хронологически
    finally:
        con.close()


def test_replay_day_refuses_when_unrecoverable(db):
    con = RS._connect_ro(db)
    try:
        d = RS.replay_day(con, "2026-01-05", {"kw"}, {"fallback": {"ret_z_20": 30.0}})
        assert "П8" in d["отказ"]
    finally:
        con.close()


def test_replay_day_two_configs_structure(db):
    con = RS._connect_ro(db)
    try:
        d = RS.replay_day(con, "2026-06-15", {"kw"},
                          {"fallback": {"ret_z_20": 30.0, "vol_z_log_20": 20.0, "vol_z_20": 3}})
        assert d["универсум_asof"] == 1 and d["инструментов_с_индикаторами"] == 1
        assert d["новостей_в_срезе"] == 1
        for label in ("старая", "новая"):
            blk = d[label]
            assert blk["price_сигналов"] == 2              # ret_z_20 + vol_z_log_20 одного AAA
            assert blk["trend_сигналов"] == 1              # у kw ≥8 точек истории
            assert isinstance(blk["после_FDR"], int)
            assert "из_них_артефактов_нулевого_объёма" in blk
        assert isinstance(d["появились"], list) and isinstance(d["исчезли"], list)
    finally:
        con.close()


def test_replay_day_trend_slice_by_date(db):
    con = RS._connect_ro(db)
    try:
        # 11.06: трендов с date<=10.06 всего 6 < MIN_TREND_HISTORY(8) → трендовых сигналов 0
        d = RS.replay_day(con, "2026-06-11", {"kw"}, {"fallback": {}})
        assert d["старая"]["trend_сигналов"] == 0
    finally:
        con.close()


def test_old_ref_with_tail_df_rejected(tmp_path, monkeypatch):
    """Старая конфигурация обязана быть БЕЗ tail_df — иначе это не «старая» (fail-closed)."""
    import subprocess

    class FakeGot:
        returncode = 0
        stdout = "fdr:\n  tail_df:\n    fallback: {ret_z_20: 30}\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeGot())
    with pytest.raises(SystemExit):
        RS._old_tail_df("какой-то-ref")
