# -*- coding: utf-8 -*-
"""Тесты агрегатора абляции §11.1 (orchestrator/ablation).

Проверяет: drop-one контрфакты по прогонам РЕАЛЬНО читаются и агрегируются в таблицу влияния
(работает без исходов); Brier-дельта корректна по знаку (помогал/шумел); вклад при N<30 честно
«накапливается» (§10), пустой вход не выдумывает вклад (П8)."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import ablation as A  # noqa: E402


def test_loads_run_counterfactuals():
    runs = A.load_run_counterfactuals()
    assert runs, "должны быть прогоны с контрфактами в journal/funnel_logs"
    for r in runs:
        assert "контрфакты" in r and isinstance(r["контрфакты"], dict)


def test_influence_table_computed_without_outcomes():
    runs = A.load_run_counterfactuals()
    tab = A.influence_table(runs)
    assert tab, "таблица влияния должна считаться по drop-one без единого исхода"
    for row in tab:
        assert row["n_участий"] >= 1
        assert row["mean_abs_shift"] is None or row["mean_abs_shift"] >= 0
    # отсортировано по убыванию |сдвига|
    shifts = [r["mean_abs_shift"] or 0 for r in tab]
    assert shifts == sorted(shifts, reverse=True)


def test_brier_delta_sign():
    # исход=1: агрегат 0.8 точнее, без X 0.5 хуже → удаление X ухудшило → X помогал (дельта>0)
    assert A.brier_delta(0.8, 0.5, 1) > 0
    # исход=0: агрегат 0.8 хуже, без X 0.5 лучше → удаление X улучшило → X шумел (дельта<0)
    assert A.brier_delta(0.8, 0.5, 0) < 0
    # нейтрально: одинаковые вероятности → нулевой вклад
    assert A.brier_delta(0.6, 0.6, 1) == 0


def test_contribution_below_n_accumulating():
    linked = [{"agent": "x", "p_agg": 0.7, "p_without": 0.5, "outcome": 1} for _ in range(5)]
    out = A.agent_brier_contribution(linked)
    assert out[0]["n_исходов"] == 5
    assert out[0]["значимо_§10"] is False
    assert "накапливается" in out[0]["вывод"]


def test_contribution_significant_at_n30():
    # 30 исходов, X стабильно помогал → значимо, вывод положительный
    linked = [{"agent": "x", "p_agg": 0.8, "p_without": 0.5, "outcome": 1} for _ in range(30)]
    out = A.agent_brier_contribution(linked)
    assert out[0]["n_исходов"] == 30
    assert out[0]["значимо_§10"] is True
    assert "ПОЛОЖИТЕЛЬНЫЙ" in out[0]["вывод"]


def test_contribution_negative_proposes_demotion():
    # X стабильно шумел (исход 0, но агрегат тянулся к 0.8) → отрицательный значимый вклад
    linked = [{"agent": "x", "p_agg": 0.8, "p_without": 0.5, "outcome": 0} for _ in range(30)]
    out = A.agent_brier_contribution(linked)
    assert out[0]["mean_brier_delta"] < 0
    assert "понижение" in out[0]["вывод"]


def test_empty_contribution_no_fabrication():
    assert A.agent_brier_contribution([]) == []


def test_run_ablation_honest_when_no_outcomes():
    s = A.run_ablation(write=False)
    assert s["n_прогонов_всего"] >= 1
    assert s["n_разрешённых_исходов_связок"] == 0      # форвард-этап не начат
    assert "накапливается" in s["вывод"]
    assert s["таблица_влияния_drop_one"], "drop-one влияние всё равно посчитано"
