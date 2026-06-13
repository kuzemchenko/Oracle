# -*- coding: utf-8 -*-
"""Тесты скорера тектонического потенциала (mathlib/tectonic.py, пилот §5/П5)."""
import math

import pytest

from mathlib import tectonic as T


def _pilot():
    c = T.get_chain("ai_power_transformers_metal")
    assert c is not None, "пилотная цепочка должна быть в knowledge/cascade_chains.yaml"
    return c


def test_components_and_potential_deterministic():
    s = T.score_chain(_pilot())
    comp = s["components"]
    assert comp["M"] == 1.0          # magnitude=high
    assert comp["P"] == 1.0          # persistence=structural
    assert comp["S"] == 0.6          # surprise=medium
    assert comp["C"] == 0.75         # 2/4 чокпоинта + 7 инструментов → 0.5*0.5 + 0.5*1.0
    assert comp["L"] == 1.0          # лаги 30+60+90=180 → полное окно
    assert comp["A"] == 0.85         # дальний чокпоинт порядка ≥3
    assert s["lag_window_days"] == 180
    assert math.isclose(s["tectonic_potential"], 0.8825, abs_tol=1e-3)


def test_best_far_node_is_deep_chokepoint():
    s = T.score_chain(_pilot())
    far = s["best_far_node"]
    # движок должен выбрать НЕОТЫГРАННОЕ дальнее чокпоинт-звено = GOES-сталь (CLF), порядок 3
    assert far["order"] == 3
    assert "CLF.US" in far["instruments"]
    assert far["chokepoint"] is True
    assert far["priced"] == 0.2
    assert math.isclose(s["entry_score"], 0.8825 * 0.8, abs_tol=1e-3)


def test_priced_override_lowers_entry():
    # если рынок уже отыграл дальнее звено (priced=0.9) — entry_score падает
    s = T.score_chain(_pilot(), priced_override={3: 0.9})
    assert s["best_far_node"]["priced"] == 0.9
    assert s["entry_score"] < 0.2


def test_missing_level_falls_to_neutral_with_note():
    chain = {"id": "x", "trigger": {}, "nodes": [
        {"order": 1, "node": "n", "instruments": ["AAA.US"], "chokepoint": False}], "edges": []}
    s = T.score_chain(chain)
    assert s["components"]["M"] == 0.5          # нет уровня → нейтраль
    assert any("нет данных" in n or "нейтраль" in n for n in s["notes"])


def test_rank_chains_returns_sorted():
    ranked = T.rank_chains()
    assert ranked and all(
        ranked[i]["entry_score"] >= ranked[i + 1]["entry_score"]
        for i in range(len(ranked) - 1))
