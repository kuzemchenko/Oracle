# -*- coding: utf-8 -*-
"""Тесты граф-строителя (orchestrator/graph_build.py, §R2 / Этап B2).

In-memory sqlite с синтетическими коррелированными рядами — бьём по РЕАЛЬНЫМ запросам quotes,
но данные контролируем. Проверяем: резолв фактов ворот (торгуемость/объём/изоляция/окно),
честный «нет данных» для несуществующего инструмента, и сквозную воронку (ворота → ранг → топ-K).
"""
import datetime
import math
import sqlite3

import numpy as np
import pytest

from orchestrator import graph_build as GB


def _mk_db(series, vol=1_000_000):
    """series: {symbol: np.array дневных лог-доходностей}. Строит closes от 100 и кладёт в quotes."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, "
                "adjusted_close REAL, volume REAL)")   # F0#8: боевая схема (adjusted_close)
    base = datetime.date(2020, 1, 1)
    for sym, rets in series.items():
        price = 100.0
        for i, r in enumerate(rets):
            price *= math.exp(float(r))
            d = (base + datetime.timedelta(days=i)).isoformat()
            con.execute("INSERT INTO quotes VALUES (?,?,?,?,?)", (sym, d, price, price, vol))
    con.commit()
    return con


def _correlated(n=160, seed=0):
    """root ~ N(0,0.02); term = 0.8·root + шум → измеримый R²."""
    rng = np.random.default_rng(seed)
    root = rng.normal(0, 0.02, n)
    term = 0.8 * root + rng.normal(0, 0.01, n)
    term2 = 0.5 * root + rng.normal(0, 0.015, n)
    return {"ROOT.US": root, "TERM.US": term, "TERM2.US": term2}


def _node(sym, *, tiers, amplitude, lag=30, rel=0.5, total=None, order=2):
    return {"узел": sym, "tiers": tiers, "amplitude": amplitude, "reliability_r2": rel,
            "lag_total": lag, "amplitude_total": total or amplitude, "order": order,
            "chokepoint": False, "research": ("A" not in tiers)}


# ── резолв фактов ───────────────────────────────────────────────────────────────────
def test_node_to_facts_resolves_gate_facts():
    con = _mk_db(_correlated())
    f = GB.node_to_facts(_node("TERM.US", tiers=["A"], amplitude=0.04, lag=30, total=0.06),
                         con=con, root_symbol="ROOT.US", horizon_days=20)
    assert f["symbol"] == "TERM.US"
    assert f["sealable"] is True                       # 160 баров ≥ 20
    assert f["adv"] == pytest.approx(1_000_000)        # постоянный объём
    assert f["lag_days"] == 30
    assert f["resolvable"] is True
    assert f["amplitude"] == 0.04
    assert f["sigma_h"] is not None and f["sigma_h"] > 0
    assert f["r2"] is not None and 0.0 <= f["r2"] <= 1.0   # измеримо (коррелированы)


def test_node_to_facts_missing_instrument_is_honest_not_invented():
    con = _mk_db(_correlated())
    f = GB.node_to_facts(_node("GHOST.US", tiers=["A"], amplitude=0.04),
                         con=con, root_symbol="ROOT.US", horizon_days=20)
    assert f["sealable"] is False                      # нет источника цены
    assert f["adv"] is None                            # нет объёма → провал ликвидности
    assert f["r2"] is None                             # нет истории → структурный дефолт в isolation
    assert f["resolvable"] is False


# ── F2#18: R² на ДАТО-ВЫРОВНЕННЫХ рядах (а не позиционный zip) ─────────────────────────
def test_isolation_r2_uses_date_alignment_not_positional_zip():
    """root и term несут ИДЕНТИЧНЫЕ доходности, но на СДВИНУТЫХ датах. Позиционный zip (старый баг)
    спарил бы их как одинаковые → ложный R²≈1. Дато-выравнивание сравнивает РАЗНЫЕ участки истории
    одного блуждания на общих датах → честный низкий R² (триггер SPCX/RKLB)."""
    rng = np.random.default_rng(7)
    rets = rng.normal(0, 0.02, 200)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, adjusted_close REAL, volume REAL)")
    base = datetime.date(2020, 1, 1)
    for sym, off in (("ROOT.US", 0), ("TERM.US", 100)):   # TERM на 100 дней позже теми же доходностями
        price = 100.0
        for i, r in enumerate(rets):
            price *= math.exp(float(r))
            d = (base + datetime.timedelta(days=off + i)).isoformat()
            con.execute("INSERT INTO quotes VALUES (?,?,?,?,?)", (sym, d, price, price, 1_000_000))
    con.commit()
    r2, sigma = GB._isolation_r2_and_sigma(con, "ROOT.US", "TERM.US")
    assert sigma is not None and sigma > 0
    # на общих датах (100 дней) сравниваются непересекающиеся куски iid-блуждания → корреляции почти нет
    assert r2 is not None and r2 < 0.3        # старый позиционный zip дал бы ≈1.0


def test_aligned_returns_insufficient_overlap_is_none():
    con = _mk_db(_correlated())
    rc, tc = GB._aligned_returns(con, "ROOT.US", "GHOST.US", GB.ISO_LOOKBACK)  # GHOST нет в БД
    assert rc is None and tc is None


# ── сквозная воронка ──────────────────────────────────────────────────────────────────
def test_select_from_nodes_keeps_low_basis_gates_only_actionable():
    con = _mk_db(_correlated())
    nodes = [
        _node("TERM.US", tiers=["A"], amplitude=0.04, rel=0.5),         # пройдёт ворота
        _node("TERM2.US", tiers=["C", "C"], amplitude=0.03, rel=0.05),  # ярус C — НЕ отсев (директива 20.06)
        _node("GHOST.US", tiers=["A"], amplitude=0.04),                 # отсев: нет инструмента/объёма
    ]
    res = GB.select_from_nodes(nodes, con=con, root_symbol="ROOT.US", horizon_days=20)
    assert res["ворота_прошли"] == 2                    # TERM.US и TERM2.US — ярус не гейтит
    crit = res["отсев_по_критериям"]
    assert "сцепление" not in crit
    assert crit.get("торгуемость") == 1                 # GHOST (нет инструмента)
    assert res["топ_k"][0]["symbol"] == "TERM.US"       # ярус A, больше амплитуда/изоляция → выше ранг


def test_route_tracks_splits_by_basis():
    # выжившие ярус-A → money; ярус-B/C → provisional; отсеянные воротами → digest_only (не seal)
    sel = {
        "ранжировано": [
            {"symbol": "A1", "node": {"research": False}},   # established → денежный трек
            {"symbol": "C1", "node": {"research": True}},     # гипотеза → провизорный трек
        ],
        "отсев": [{"symbol": "G1", "fails": [("торгуемость", "нет инструмента")]}],
    }
    r = GB.route_tracks(sel)
    assert [x["symbol"] for x in r["money"]] == ["A1"]
    assert [x["symbol"] for x in r["provisional"]] == ["C1"]
    assert [x["symbol"] for x in r["digest_only"]] == ["G1"]


def test_build_graph_orchestration(monkeypatch):
    con = _mk_db(_correlated())

    def _stub_build(ch, shock, *, horizon_days, con, db=None):    # подменяем боевой cascade_build
        return {"chain_id": ch["id"],
                "узлы": [_node("TERM.US", tiers=["A"], amplitude=0.04, lag=25)]}

    monkeypatch.setattr(GB.CB, "build_from_db", _stub_build)
    res = GB.build_graph("ROOT.US", -0.05, con=con, horizon_days=20,
                         chains=[{"id": "t_chain"}])
    assert res["цепочки"] == ["t_chain"]
    assert res["граф_узлов"] == 1
    assert res["отбор"]["ворота_прошли"] == 1
    assert res["отбор"]["топ_k"][0]["symbol"] == "TERM.US"
