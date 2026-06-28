# -*- coding: utf-8 -*-
"""Тесты детерминированного отбора узлов графа (mathlib/graph_select.py, §R2 / Этап B1).

Чистая математика — все входы явные, без БД/LLM/rng. Проверяем: каждый критерий ворот валит
по отдельности; snr/изоляция честны (нет истории → структурный дефолт, не выдумка); коллизия
штрафует; пред-ранг мультипликативен (любой ноль убивает); select сортирует и логирует отсев.
"""
import math

import pytest

from mathlib import graph_select as GS


def _ok_node(**over):
    """Узел, проходящий ВСЕ ворота (база для точечной мутации одного критерия)."""
    n = dict(symbol="AAA", sealable=True, adv=1_000_000, lag_days=30, resolvable=True,
             tiers=["A", "A"], amplitude=0.06, resid_std=0.02, horizon_days=20,
             reliability=0.8, r2=0.5)
    n.update(over)
    return n


# ── ворота ───────────────────────────────────────────────────────────────────────────
def test_gate_all_pass():
    g = GS.gate_node(sealable=True, adv=1e6, lag_days=30, resolvable=True)
    assert g["passed"] and g["fails"] == []


@pytest.mark.parametrize("over,crit", [
    (dict(sealable=False), "торгуемость"),
    (dict(adv=50), "ликвидность"),
    (dict(adv=None), "ликвидность"),
    (dict(lag_days=999), "окно"),
    (dict(lag_days=None), "окно"),
    (dict(lag_days=-1), "окно"),
    (dict(resolvable=False), "разрешимость"),
])
def test_gate_each_criterion_fails(over, crit):
    base = dict(sealable=True, adv=1e6, lag_days=30, resolvable=True)
    base.update(over)
    g = GS.gate_node(**base)
    assert not g["passed"]
    assert crit in [c for c, _ in g["fails"]]


def test_tier_does_not_gate():
    # директива 20.06: нехватка эмпирики НЕ отбрасывает — ворота про ДЕЙСТВИЕ, не про ярус
    assert GS.gate_node(sealable=True, adv=1e6, lag_days=10, resolvable=True)["passed"]


# ── вола / сигнал-над-шумом ────────────────────────────────────────────────────────────
def test_sigma_horizon():
    assert GS.sigma_horizon(0.02, 4) == pytest.approx(0.04)      # 0.02·√4
    assert GS.sigma_horizon(None, 10) is None


def test_signal_to_noise():
    assert GS.signal_to_noise(0.06, 0.03) == pytest.approx(2.0)
    assert GS.signal_to_noise(-0.06, 0.03) == pytest.approx(2.0)  # по модулю
    assert GS.signal_to_noise(0.05, 0) is None                    # нет волы
    assert GS.signal_to_noise(None, 0.03) is None


# ── коллизия ──────────────────────────────────────────────────────────────────────────
def test_collision_count():
    assert GS.collision_count("xyz", ["XYZ", "ABC", "xyz"]) == 1   # сам узел не считается
    assert GS.collision_count("ZZZ", ["AAA", "BBB"]) == 0


# ── изоляция ──────────────────────────────────────────────────────────────────────────
def test_isolation_with_r2():
    iso = GS.isolation_factor(0.5)
    assert iso["factor"] == pytest.approx(0.5)
    assert iso["collisions"] == 0


def test_isolation_no_history_uses_structural_default_not_invention():
    iso = GS.isolation_factor(None)
    assert iso["factor"] == pytest.approx(GS.STRUCTURAL_ISOLATION)
    assert "неизмерим" in iso["провенанс"]                         # честно помечено (П8)


def test_isolation_collision_discount():
    base = GS.isolation_factor(0.8, collisions=0)["factor"]
    one = GS.isolation_factor(0.8, collisions=1)["factor"]
    assert one < base
    assert one == pytest.approx(0.8 / (1 + GS.COLLISION_LAMBDA))   # дисконт 1/(1+λ·k)


# ── пред-ранг: мультипликативное вето ──────────────────────────────────────────────────
def test_prerank_reliability_is_label_not_buryer():
    # директива 20.06: надёжность НЕ давит discovery-ранг — тот же edge → тот же score
    hi = GS.prerank(amplitude=0.06, sigma_h=0.03, reliability=0.8, r2=0.5)
    lo = GS.prerank(amplitude=0.06, sigma_h=0.03, reliability=0.008, r2=0.5)
    assert hi["score"] == lo["score"]
    assert lo["reliability"] == 0.008             # но несётся как метка уверенности


def test_prerank_no_vol_kills():
    pr = GS.prerank(amplitude=0.06, sigma_h=None, reliability=0.8, r2=0.5)
    assert pr["score"] == 0.0 and pr["snr"] is None and pr["причина_ноль"]


def test_prerank_value_and_monotonic():
    pr = GS.prerank(amplitude=0.06, sigma_h=0.03, reliability=0.8, r2=0.5)
    assert pr["score"] == pytest.approx(2.0 * 0.5)                # snr=2 × изоляция (надёжность вне ранга)
    bigger = GS.prerank(amplitude=0.12, sigma_h=0.03, reliability=0.8, r2=0.5)
    assert bigger["score"] > pr["score"]                         # больше неотыгранный edge → выше


# ── сквозной select ────────────────────────────────────────────────────────────────────
def test_select_ranks_deep_node_by_opportunity_not_basis():
    nodes = [
        _ok_node(symbol="STRONG", amplitude=0.06),                                          # ярус A, высокий edge
        _ok_node(symbol="DEEPC", tiers=["C", "C", "C"], reliability=0.008, amplitude=0.06),  # ярус C, ТА ЖЕ возможность
        _ok_node(symbol="WEAK", amplitude=0.008),                                           # малый edge → ниже
        _ok_node(symbol="NOINSTR", sealable=False),                                         # отсев: торгуемость
        _ok_node(symbol="ILLIQ", adv=10),                                                   # отсев: ликвидность
    ]
    res = GS.select(nodes, top_k=2)
    assert res["всего"] == 5
    assert res["ворота_прошли"] == 3                                     # ярус C НЕ гейтит (директива 20.06)
    assert res["отсев_по_критериям"] == {"торгуемость": 1, "ликвидность": 1}
    by = {s["symbol"]: s["score"] for s in res["ранжировано"]}
    assert by["DEEPC"] == by["STRONG"]                                   # ярус C НЕ похоронен: тот же edge → тот же ранг
    assert by["WEAK"] < by["STRONG"]                                     # рангуем по edge-возможности, не по ярусу
    assert res["ранжировано"][-1]["symbol"] == "WEAK"                    # малый edge — на дне, а не дальний узел


def test_select_dedupes_by_ticker_best_tier_then_score():
    # несколько путей в один тикер → один узел, ЛУЧШИЙ ЯРУС (money-путь) важнее провизорного
    nodes = [
        _ok_node(symbol="DUP", research=True, amplitude=0.05),    # провизорный путь
        _ok_node(symbol="DUP", research=False, amplitude=0.05),   # money-путь — побеждает (тот же score)
        _ok_node(symbol="SOLO", research=False, amplitude=0.04),
    ]
    res = GS.select(nodes, top_k=8)
    syms = [s["symbol"] for s in res["ранжировано"]]
    assert syms.count("DUP") == 1                                 # схлопнут к одному
    dup = next(s for s in res["ранжировано"] if s["symbol"] == "DUP")
    assert dup["node"]["research"] is False                       # оставлен money-путь (лучший ярус)
    assert "DUP" in res["дедуп_отброшено"]


def test_select_collision_penalises_crowded_instrument():
    # два РАЗНЫХ каскада (разные ярусы/амплитуды роли не играют) метят в один тикер CROWD;
    # одиночный LONE с теми же числами должен оказаться не ниже из-за отсутствия коллизии
    nodes = [
        _ok_node(symbol="CROWD", amplitude=0.05),
        _ok_node(symbol="CROWD", amplitude=0.05),                 # коллизия по CROWD
        _ok_node(symbol="LONE", amplitude=0.05),
    ]
    res = GS.select(nodes, top_k=3)
    by = {}
    for s in res["ранжировано"]:
        by.setdefault(s["symbol"], s["score"])
    assert by["LONE"] > by["CROWD"]                               # одиночка не оштрафован коллизией


def test_select_empty():
    res = GS.select([])
    assert res["всего"] == 0 and res["топ_k"] == [] and res["отсев_по_критериям"] == {}
