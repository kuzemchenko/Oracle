# -*- coding: utf-8 -*-
"""Тесты динамического резолва инструмента (orchestrator/cascade_resolve.py, Этап 4 §9).

Гейт: узел с установленным переносом И §9-разрешимым инструментом → §9-прогноз (все поля,
проходит SEAL.validate_resolvable); узел без переноса / без источника цены → лист ожидания (П8).
"""
import sys
import sqlite3
import datetime
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import cascade_resolve as CR    # noqa: E402
from mathlib import sealing as SEAL               # noqa: E402

NOW = datetime.datetime(2026, 6, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _db(tmp_path):
    db = tmp_path / "q.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL)")
    con.executemany("INSERT INTO quotes VALUES (?,?,?)",
                    [("BNO.US", f"2026-04-{i+1:02d}", 47.0 + i * 0.1) for i in range(40)])
    con.commit()
    con.close()
    return db


def _node(symbol, sealable, amplitude, prob, reason="перенос установлен"):
    return {"узел": symbol, "sealable": sealable, "причина": reason,
            "amplitude": amplitude, "probability": prob, "reliability_r2": 0.9}


def test_sealable_node_becomes_valid_prediction(tmp_path):
    db = _db(tmp_path)
    node = _node("BNO.US", True, amplitude=-0.046, prob=0.02)   # шорт-каскад
    r = CR.resolve_node(node, run_id="r1", horizon_days=5, now_dt=NOW, db=db)
    assert r["kind"] == "seal"
    pred = r["prediction"]
    assert SEAL.validate_resolvable(pred) == []                 # все §9-поля на месте
    assert pred["direction"] == "below"                         # знак амплитуды
    assert 0.0 <= pred["probability"] <= 1.0
    assert pred["asset"] == "BNO.US" and pred["price_source"].startswith("EODHD")


def test_transmission_not_established_to_watchlist(tmp_path):
    db = _db(tmp_path)
    node = _node("BNO.US", False, amplitude=-0.001, prob=0.49,
                 reason="перенос статистически не установлен (CI корреляции включает 0) — П8")
    r = CR.resolve_node(node, run_id="r1", horizon_days=5, now_dt=NOW, db=db)
    assert r["kind"] == "watchlist"
    assert "не установлен" in r["причина"]


def test_no_price_source_to_watchlist(tmp_path):
    db = _db(tmp_path)
    node = _node("XYZ.US", True, amplitude=0.05, prob=0.8)      # нет в quotes
    r = CR.resolve_node(node, run_id="r1", horizon_days=5, now_dt=NOW, db=db)
    assert r["kind"] == "watchlist"
    assert "§9-источника" in r["причина"]


def test_zero_amplitude_to_watchlist(tmp_path):
    db = _db(tmp_path)
    node = _node("BNO.US", True, amplitude=0.0, prob=0.5)
    r = CR.resolve_node(node, run_id="r1", horizon_days=5, now_dt=NOW, db=db)
    assert r["kind"] == "watchlist"


def test_resolve_cascade_splits(tmp_path):
    db = _db(tmp_path)
    casc = {"источник": "USO.US", "shock": -0.05, "horizon_days": 5, "узлы": [
        _node("BNO.US", True, -0.046, 0.02),
        _node("SPY.US", False, -0.0002, 0.50, reason="перенос не установлен"),
        _node("XYZ.US", True, 0.05, 0.8),
    ]}
    out = CR.resolve_cascade(casc, run_id="r1", now_dt=NOW, db=db)
    assert len(out["запечатываемо"]) == 1
    assert len(out["лист_ожидания"]) == 2
    assert out["запечатываемо"][0]["узел"] == "BNO.US"
    assert SEAL.is_resolvable(out["запечатываемо"][0]["prediction"])
