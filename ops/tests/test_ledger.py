# -*- coding: utf-8 -*-
"""Общий счёт предложений и сделок (ревью 14.11): модель входа, эталон 60/40, сделки отдельно, П8."""
import pytest

from mathlib import sealing as SEAL
from ops import ledger as L


def _series(table):
    return lambda sym, d0: [(d, c) for d, c in table.get(sym, []) if d >= d0]


TABLE = {
    "AAA.US": [("2026-11-02", 10.0), ("2026-11-10", 12.0)],
    "SPY.US": [("2026-11-02", 100.0), ("2026-11-10", 105.0)],
    "DBC.US": [("2026-11-02", 20.0), ("2026-11-10", 20.0)],
    "BRNT.LSE": [("2026-11-10", 93.0), ("2026-11-13", 95.0)],
}


def _seal(pp):
    return SEAL.seal({"kind": "hunter_idea", "asset": "AAA.US", "direction": "above", "threshold": 13.0,
                      "resolve_by": "2026-12-20T20:00:00+00:00", "price_source": "EODHD close AAA.US",
                      "якорь_цена": 10.0, "якорь_дата": "2026-11-02"}, path=pp)


def test_sealed_proposal_against_benchmark(tmp_path):
    pp = tmp_path / "p.jsonl"
    _seal(pp)
    rep = L.build(L.Prices(fetch=_series(TABLE)), pred_path=pp, manual_path=tmp_path / "m.jsonl",
                  trades_path=tmp_path / "t.jsonl")
    r = rep["предложения"][0]
    assert r["доходность"] == pytest.approx(0.2)
    assert r["эталон"] == pytest.approx(0.6 * 0.05)                  # 60% SPY +5%, 40% DBC 0%
    assert r["сверх_эталона"] == pytest.approx(0.17)
    assert "рано судить" in rep["итог"]["вывод"]
    assert "эталон" in L.render(rep)


def test_voided_proposal_not_counted(tmp_path):
    pp = tmp_path / "p.jsonl"
    rec = _seal(pp)
    SEAL.void(rec["hash"], "тест", "слова", predictions_path=pp)
    rep = L.build(L.Prices(fetch=_series(TABLE)), pred_path=pp, manual_path=tmp_path / "m.jsonl",
                  trades_path=tmp_path / "t.jsonl")
    assert rep["предложения"] == []


def test_trade_needs_source_and_is_separate_from_model(tmp_path):
    tp = tmp_path / "t.jsonl"
    with pytest.raises(ValueError):
        L.add_trade("BRNT.LSE", "buy", 2, 93.61, path=tp)
    L.add_trade("BRNT.LSE", "buy", 2, 93.61, fee=4, date="2026-11-10", source="подтверждение брокера",
                path=tp)
    rep = L.build(L.Prices(fetch=_series(TABLE)), pred_path=tmp_path / "p.jsonl",
                  manual_path=tmp_path / "m.jsonl", trades_path=tp)
    p = rep["позиции"][0]
    assert p["кол_во"] == 2 and p["стоимость"] == pytest.approx(190.0)
    assert p["результат_до_комиссий"] == pytest.approx(190.0 - 187.22)
    assert rep["предложения"] == []                                   # сделка — не предложение модели


def test_missing_price_is_honest(tmp_path):
    mp = tmp_path / "m.jsonl"
    L.add_proposal("ZZZ", "above", "2026-11-09", source="chat_agent.jsonl:58", path=mp)
    rep = L.build(L.Prices(fetch=_series(TABLE)), pred_path=tmp_path / "p.jsonl", manual_path=mp,
                  trades_path=tmp_path / "t.jsonl")
    assert rep["предложения"][0]["нет_цены"] is True
    assert "нет цены" in L.render(rep)


def test_manual_proposal_needs_source(tmp_path):
    with pytest.raises(ValueError):
        L.add_proposal("EQT", "above", "2026-11-09", path=tmp_path / "m.jsonl")


def test_stop_closes_paper_position(tmp_path):
    pp = tmp_path / "p.jsonl"
    SEAL.seal({"kind": "hunter_idea", "asset": "AAA.US", "direction": "above", "threshold": 13.0,
               "resolve_by": "2026-12-20T20:00:00+00:00", "price_source": "EODHD close AAA.US",
               "якорь_цена": 10.0, "якорь_дата": "2026-11-02", "стоп": 9.0}, path=pp)
    table = dict(TABLE, **{"AAA.US": [("2026-11-02", 10.0), ("2026-11-05", 8.5), ("2026-11-10", 12.0)]})
    rep = L.build(L.Prices(fetch=_series(table)), pred_path=pp, manual_path=tmp_path / "m.jsonl",
                  trades_path=tmp_path / "t.jsonl")
    r = rep["предложения"][0]
    assert r["по_стопу"] is True and r["доходность"] == pytest.approx(-0.15) and r["на_дату"] == "2026-11-05"
    assert "вышли по стопу" in L.render(rep)
