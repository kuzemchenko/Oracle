# -*- coding: utf-8 -*-
"""Тесты условного оценивателя переноса (этап Д3, mathlib/calibration/conditional.py).

Синтетика (Инв#6, §23.1): вшитый ЭПИЗОДНЫЙ перенос (цель реагирует на всплески источника
с лагом) находится с правильным лагом и знаком/величиной gain; чистый шум — «не установлено»
(П8); перенос, существующий только в train-эре, walk-forward отбраковывает. Плюс тесты-стражи
консистентности порога эпизода с активацией B4 (решение владельца 13.07, Вопрос 4)."""
import sys
import pathlib

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mathlib import cascade as CAS                        # noqa: E402
from mathlib.calibration import conditional as COND       # noqa: E402

N = 3000
LAG = 2
GAIN = 0.6


def _synthetic(transfer_until=None, seed=7):
    """Источник: шум + всплески ±0.05/день по 5 дней каждые 40 дней. Цель: шум + GAIN×всплеск
    источника с лагом LAG (реагирует ТОЛЬКО на всплески — эпизодный перенос), до transfer_until."""
    rng = np.random.default_rng(seed)
    src = rng.normal(0.0, 0.008, N)
    burst = np.zeros(N)
    for k, start in enumerate(range(100, N - 20, 40)):
        sign = 1.0 if k % 2 == 0 else -1.0
        burst[start:start + 5] = sign * 0.05
    src = src + burst
    tgt = rng.normal(0.0, 0.004, N)
    resp = np.zeros(N)
    resp[LAG:] = GAIN * burst[:-LAG]
    if transfer_until is not None:
        resp[transfer_until:] = 0.0
    return src, tgt + resp


def test_embedded_episodic_transfer_found_with_lag_and_gain():
    src, tgt = _synthetic()
    rec = COND.estimate_pair(src, tgt)
    assert rec["wf_established"] is True
    assert rec["status"] == "установлено"
    assert rec["lag_selected"] == LAG                      # лаг найден точно
    # gain: правильный знак и величина (аттенюация из-за шумовых эпизодов без переноса —
    # свойство синтетики, допускаем коридор вокруг вшитого 0.6)
    assert rec["gain_conditional"] is not None
    assert 0.3 <= rec["gain_conditional"] <= 0.8
    assert rec["tier"] in ("A", "B")
    assert rec["n_episodes"] > 0
    assert rec["n_episodes_oos"] >= COND.TIER_B_MIN_OOS_EPISODES


def test_pure_noise_not_established():
    rng = np.random.default_rng(11)
    src = rng.normal(0.0, 0.01, N)
    tgt = rng.normal(0.0, 0.01, N)
    rec = COND.estimate_pair(src, tgt)
    assert rec["wf_established"] is False
    assert rec["status"] == "не установлено"
    assert rec["gain_conditional"] is None                 # П8: величина не выдумывается
    assert rec["lag_selected"] is None
    assert rec["tier"] == "C"


def test_walkforward_rejects_train_only_transfer():
    # перенос существует только в первых 40% ряда → поздние OOS-фолды его не подтверждают
    src, tgt = _synthetic(transfer_until=int(N * 0.4))
    rec = COND.estimate_pair(src, tgt)
    assert rec["wf_established"] is False
    assert rec["status"] == "не установлено"
    assert rec["gain_conditional"] is None


def test_short_history_is_no_data():
    rng = np.random.default_rng(3)
    src = rng.normal(0.0, 0.01, 200)
    tgt = rng.normal(0.0, 0.01, 200)
    rec = COND.estimate_pair(src, tgt)
    assert rec["status"] == "не установлено"
    assert "нет данных" in rec["провенанс"]


def test_shock_episodes_threshold_and_non_overlap():
    src, _ = _synthetic()
    eps = COND.shock_episodes(src)
    assert eps, "на синтетике со всплесками эпизоды обязаны найтись"
    for e in eps:
        assert abs(e["shock"]) >= e["threshold"] - 1e-12
        assert e["threshold"] == COND.SHOCK_SIGMA_FRAC * e["sigma"] * np.sqrt(COND.EVENT_WINDOW_DAYS)
    ts = [e["t"] for e in eps]
    assert all(b - a >= COND.EVENT_WINDOW_DAYS for a, b in zip(ts, ts[1:]))  # без псевдорепликации


def test_lag_response_out_of_range_is_none():
    r = np.zeros(50)
    assert COND.lag_response(r, 48, 5) is None             # окно за краем ряда → «не измерено»
    assert COND.lag_response(r, 2, 0) is None              # окно до начала ряда
    assert COND.lag_response(r, 10, 0) == 0.0


def test_tier_mapping_documented():
    # маппинг N_эпизодов → ярус (фиксирован до прогона; продублирован в отчёте Д3)
    assert COND._tier(True, COND.TIER_A_MIN_OOS_EPISODES) == "A"
    assert COND._tier(True, COND.TIER_A_MIN_OOS_EPISODES - 1) == "B"
    assert COND._tier(True, COND.TIER_B_MIN_OOS_EPISODES - 1) == "C"
    assert COND._tier(False, 10 ** 6) == "C"               # без wf-устойчивости ярус не выдаётся


def test_episode_threshold_consistent_with_b4_activation():
    """Страж решения владельца 13.07 (Вопрос 4): порог эпизода Д3 = порог активации B4
    (|shock| ≥ 0.5·σ_ист·√окна, σ по 61 бару = 60 доходностям, окно §R2.1)."""
    from orchestrator import edge_forward as EF
    assert COND.SHOCK_SIGMA_FRAC == EF.SHOCK_SIGMA_FRAC
    assert COND.SIGMA_RETURNS == EF.SIGMA_BARS - 1         # 61 бар ⇔ 60 доходностей
    assert COND.EVENT_WINDOW_DAYS == CAS.EVENT_WINDOW_DAYS


def test_t_quantile_bisection_sane():
    # df→∞ квантиль 97.5% → 1.96; малый df — тяжелее хвост (больше квантиль)
    assert abs(COND._t_ppf_975(10 ** 6) - 1.96) < 0.01
    assert COND._t_ppf_975(5) > 2.5
