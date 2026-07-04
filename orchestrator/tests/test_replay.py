# -*- coding: utf-8 -*-
"""Тесты replay-режима (долг F3/П2а, ночная смена 04.07): отсутствие look-ahead (П16)."""
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import cascade_build as CB   # noqa: E402
from orchestrator import event_first as EF     # noqa: E402


def _db(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, open REAL, high REAL, low REAL,"
                " close REAL, adjusted_close REAL, volume INTEGER)")
    con.executemany("INSERT INTO quotes (symbol, date, close, adjusted_close, volume)"
                    " VALUES (?,?,?,?,1000000)", rows)
    return con


def _series(sym, n, base, future_wild=0):
    rows = [(sym, f"2026-{3 + i // 28:02d}-{i % 28 + 1:02d}", base + i * 0.1, base + i * 0.1)
            for i in range(n)]
    for j in range(future_wild):                       # «будущее» с дикими значениями
        rows.append((sym, f"2026-06-{21 + j:02d}", base * 50, base * 50))
    return rows


def test_build_from_db_asof_equals_truncated_db(monkeypatch):
    # Ядро П16-гарантии: расчёт с asof=D на ПОЛНОЙ БД (с диким будущим) обязан быть
    # ТОЖДЕСТВЕНЕН расчёту на БД, физически усечённой по D. Чувствительность — фикс-бета
    # (on_the_fly вне объёма asof v1 и читает свою БД — изолируем).
    monkeypatch.setattr(CB.SEN, "on_the_fly",
                        lambda up, down, lag=0, db=None: {"источник": up, "узел": down, "lag": lag,
                                                          "pinned": True, "beta_pinned": 0.5,
                                                          "r2": 0.6, "n_obs": 100,
                                                          "provenance": "тест"})
    chain = {"id": "t", "nodes": [{"order": 1, "node": "root", "instruments": ["AAA.US"]},
                                  {"order": 2, "node": "leaf", "instruments": ["BBB.US"]}]}
    cutoff = "2026-06-20"
    full_rows = _series("AAA.US", 80, 10) + _series("BBB.US", 80, 20, future_wild=5)
    trunc_rows = [r for r in full_rows if r[1] <= cutoff]
    full, trunc = _db(full_rows), _db(trunc_rows)
    a = CB.build_from_db(chain, 0.03, horizon_days=5, con=full, promotions={}, asof=cutoff)
    b = CB.build_from_db(chain, 0.03, horizon_days=5, con=trunc, promotions={})
    assert a.get("узлы"), "узлы должны построиться"
    assert a["узлы"] == b["узлы"]                      # дикое будущее НЕ просочилось (П16)
    # и что без asof будущее ДЕЙСТВИТЕЛЬНО меняет результат (тест не тривиален)
    c = CB.build_from_db(chain, 0.03, horizon_days=5, con=full, promotions={})
    assert c["узлы"] != a["узлы"]


def test_run_replay_validates_cutoff_and_never_seals(tmp_path, monkeypatch):
    assert "ОТКАЗ" in EF.run_replay("не-дата", write=False)
    assert "ОТКАЗ" in EF.run_replay(None, write=False)
    # протокол replay не пишет в боевые funnel_logs и не зовёт seal
    called = []
    monkeypatch.setattr(EF.FC, "seal_prediction", lambda *a, **k: called.append(1))
    monkeypatch.setattr(EF, "REPLAY_LOGS", tmp_path)
    r = EF.run_replay("2026-06-20", write=True)
    assert r.get("REPLAY") is True and not called      # запечатывания не было
    files = list(tmp_path.glob("replay_*.json"))
    assert len(files) == 1                             # протокол — только в replay_logs
    assert "границы_честности" in r and len(r["границы_честности"]) == 4
