# -*- coding: utf-8 -*-
"""Тесты детерминированных поведенческих прокси (mathlib/behavioral.py, §R4 / R4a).

Чистая математика — входы явные. Проверяем: borrow-давление усредняет ДОСТУПНЫЕ компоненты и
честно None без данных (П8); перегрев — z от средней; внимание — log-всплеск. Прокси = ЧИСЛО.
"""
import math

import pytest

from mathlib import behavioral as B


# ── borrow-давление ───────────────────────────────────────────────────────────────────
def test_borrow_pressure_all_inputs():
    r = B.borrow_pressure(short_pct_float=0.20, shares_short=110, shares_short_prior=100,
                          short_ratio=8.0, put_skew=1.0)
    assert r["n_inputs"] == 4
    assert 0.0 <= r["score"] <= 1.0
    assert r["компоненты"]["уровень_шорта"] == pytest.approx(1.0)     # 20% → максимум
    assert r["компоненты"]["Δ_шорта"] > 0.5                           # наращивают шорт (110>100)


def test_borrow_pressure_partial_and_empty():
    # только short%float — учтён один компонент, остальные None не ломают (П8)
    r = B.borrow_pressure(short_pct_float=0.10)
    assert r["n_inputs"] == 1 and r["score"] == pytest.approx(0.5)
    # ни одного входа → честно None
    assert B.borrow_pressure()["score"] is None


def test_borrow_pressure_covering_lowers():
    building = B.borrow_pressure(shares_short=120, shares_short_prior=100)["компоненты"]["Δ_шорта"]
    covering = B.borrow_pressure(shares_short=80, shares_short_prior=100)["компоненты"]["Δ_шорта"]
    assert covering < 0.5 < building                                  # крывают ниже, наращивают выше


# ── перегрев ──────────────────────────────────────────────────────────────────────────
def test_overextension_overbought_and_oversold():
    up = list(range(1, 101))                                         # сильный аптренд
    assert B.overextension(up, window=50) > 0                        # перекуплено
    down = list(range(100, 0, -1))
    assert B.overextension(down, window=50) < 0                      # перепродано


def test_overextension_insufficient_or_flat():
    assert B.overextension([1, 2, 3], window=50) is None             # мало истории
    assert B.overextension([5.0] * 60, window=50) is None            # нулевая дисперсия


# ── внимание ──────────────────────────────────────────────────────────────────────────
def test_attention_spike_and_missing():
    assert B.attention(now=300, baseline=100) == pytest.approx(math.log(3), abs=1e-4)   # всплеск
    assert B.attention(now=50, baseline=100) < 0                              # затухание
    assert B.attention(now=None, baseline=100) is None
    assert B.attention(now=10, baseline=0) is None                            # нет фона (П8)


# ── свод ──────────────────────────────────────────────────────────────────────────────
def test_behavioral_context_combines_available():
    ctx = B.behavioral_context(prices=list(range(1, 101)),
                               short={"short_pct_float": 0.15, "short_ratio": 5.0},
                               options={"put_skew": 0.4},
                               attention_pair={"now": 200, "baseline": 100})
    assert ctx["borrow_давление"]["score"] is not None
    assert ctx["перегрев"] > 0
    assert ctx["всплеск_внимания"] > 0
