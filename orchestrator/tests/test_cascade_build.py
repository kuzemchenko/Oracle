# -*- coding: utf-8 -*-
"""Этап 4 §3c: сборка узлов-КОМПАНИЙ из авторской цепочки. Доступ к данным — стабами (без БД).

Проверяем: якорь→цепочка; ранг по непрокинутому edge (наименее отыгранный дальний узел выше);
механизм-звенья → research-only (не sealable); пин по всем звеньям → sealable; узел без данных
выпадает честно (П8).
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import cascade_build as CB   # noqa: E402

CHAIN = {
    "id": "demo",
    "nodes": [
        {"order": 1, "instruments": ["A.US"], "chokepoint": False},
        {"order": 2, "instruments": ["B.US"], "chokepoint": True},
        {"order": 3, "instruments": ["C.US"], "chokepoint": True},
    ],
    "edges": [{"from": 1, "to": 2, "lag_days": 30}, {"from": 2, "to": 3, "lag_days": 60}],
}

PINNED = {"pinned": True, "beta_pinned": 1.1, "rel_dispersion": 0.1,
          "r2_fullsample": 0.9, "provenance": "пин"}


def _has_all(_sym):
    return True


def _no_sens(_up, _down, _lag):
    return None                      # нет калибровки → механизм-гипотеза (ярус C)


def _realized(mapping):
    return lambda sym, _h: mapping.get(sym, 0.0)


def _vol(_sym):
    return 0.02


def test_chains_for_source_matches_anchor():
    assert [c["id"] for c in CB.chains_for_source("A.US", [CHAIN])] == ["demo"]
    assert CB.chains_for_source("C.US", [CHAIN]) == []   # не якорь — не активирует цепочку


def test_mechanism_links_are_research_only_and_ranked_by_unpriced_edge():
    # B уже почти отыграл шок, C — нет → C (дальний непрокинутый чокпоинт) должен быть выше
    built = CB.build_chain_nodes(
        CHAIN, shock0=0.02, horizon_days=5,
        sensitivity_fn=_no_sens, realized_fn=_realized({"B.US": 0.018, "C.US": 0.0}),
        vol_fn=_vol, has_data_fn=_has_all)
    syms = [n["узел"] for n in built["узлы"]]
    assert syms[0] == "C.US"                       # наименее отыгранный дальний узел — первым
    top = built["узлы"][0]
    assert top["research"] is True and top["sealable"] is False
    assert top["lowest_tier"] == "C"
    assert top["probability"] is not None


def test_all_pinned_path_is_sealable():
    built = CB.build_chain_nodes(
        CHAIN, shock0=-0.05, horizon_days=5,
        sensitivity_fn=lambda u, d, l: PINNED, realized_fn=_realized({}),
        vol_fn=_vol, has_data_fn=_has_all)
    node2 = next(n for n in built["узлы"] if n["узел"] == "B.US")
    assert node2["sealable"] is True               # все звенья A, перенос установлен
    assert node2["lowest_tier"] == "A"


def test_node_without_data_dropped():
    built = CB.build_chain_nodes(
        CHAIN, shock0=0.02, horizon_days=5,
        sensitivity_fn=_no_sens, realized_fn=_realized({}),
        vol_fn=_vol, has_data_fn=lambda s: s != "C.US")   # у C нет данных
    syms = [n["узел"] for n in built["узлы"]]
    assert "C.US" not in syms and "B.US" in syms


def test_amplitude_is_unpriced_edge_not_total():
    built = CB.build_chain_nodes(
        CHAIN, shock0=0.02, horizon_days=5,
        sensitivity_fn=_no_sens, realized_fn=_realized({"B.US": 0.012}),
        vol_fn=_vol, has_data_fn=_has_all)
    b = next(n for n in built["узлы"] if n["узел"] == "B.US")
    # амплитуда_total ≈ 0.02 (shock×gain 1.0), отыграно 0.012 → edge ≈ 0.008
    assert abs(b["amplitude_total"] - 0.02) < 1e-6
    assert abs(b["amplitude"] - 0.008) < 1e-6


# ── Форвард-промоушен рёбер C→A (решение 28.06.2026) ──────────────────────────────────
from mathlib.calibration import forward_promotion as FP   # noqa: E402


def test_path_edges_populated_single_and_multi():
    built = CB.build_chain_nodes(
        CHAIN, shock0=0.02, horizon_days=5,
        sensitivity_fn=_no_sens, realized_fn=_realized({}),
        vol_fn=_vol, has_data_fn=_has_all)
    b = next(n for n in built["узлы"] if n["узел"] == "B.US")
    c = next(n for n in built["узлы"] if n["узел"] == "C.US")
    assert len(b["path_edges"]) == 1               # order-2 = однозвенный путь (чистая атрибуция)
    assert b["path_edges"][0]["from"] == "A.US" and b["path_edges"][0]["to"] == "B.US"
    assert b["path_edges"][0]["lag"] == 30
    assert len(c["path_edges"]) == 2               # order-3 = композитный путь


def test_forward_promotion_makes_single_edge_node_tier_a():
    key = FP.edge_key("A.US", "B.US", 30)
    promotions = {key: {"promote": True, "reliability": 0.5, "beta_fullsample": 1.2,
                        "n": 30, "hit_rate": 0.8, "brier": 0.12}}
    built = CB.build_chain_nodes(
        CHAIN, shock0=-0.05, horizon_days=5,
        sensitivity_fn=_no_sens,             # исторически НЕ пинится (ярус C без промоушена)
        realized_fn=_realized({}), vol_fn=_vol, has_data_fn=_has_all,
        promotions=promotions)
    b = next(n for n in built["узлы"] if n["узел"] == "B.US")
    assert b["lowest_tier"] == "A"                 # ребро заработало ярус A форвардом
    assert b["sealable"] is True                   # однозвенный all_A путь → sealable
    # C.US (order-3) использует ещё и непромоутированное ребро B→C → НЕ sealable
    c = next(n for n in built["узлы"] if n["узел"] == "C.US")
    assert c["sealable"] is False
    assert c["lowest_tier"] == "C"


def test_historical_pin_takes_precedence_over_promotion():
    key = FP.edge_key("A.US", "B.US", 30)
    promotions = {key: {"promote": True, "reliability": 0.5, "beta_fullsample": 9.9}}
    link = CB._link_from_sensitivity("A.US", "B.US", 30, PINNED, promotions=promotions)
    assert link["tier"] == "A"
    assert link["gain"] == 1.1                      # бета ИСТОРИЧЕСКОГО пина, не промоушена
    assert "пин" in link["провенанс"]


def test_promotion_only_applies_to_exact_edge_key():
    promotions = {FP.edge_key("A.US", "B.US", 999): {"promote": True, "reliability": 0.5}}
    link = CB._link_from_sensitivity("A.US", "B.US", 30, None, promotions=promotions)
    assert link["tier"] == "C"                      # лаг не совпал → промоушен не применён
