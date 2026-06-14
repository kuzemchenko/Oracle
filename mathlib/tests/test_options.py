# -*- coding: utf-8 -*-
"""Тесты свёртки опционной цепочки (mathlib/options.py)."""
from mathlib import options as O


def _chain():
    # две экспирации; путы дороже по IV (страх), OI смещён в путы
    return [
        # ближняя экспирация (dte~30), spot=100
        {"type": "call", "strike": 100, "exp_date": "2026-07-15", "dte": 30,
         "volatility": 0.40, "delta": 0.50, "open_interest": 100, "volume": 10},
        {"type": "put", "strike": 100, "exp_date": "2026-07-15", "dte": 30,
         "volatility": 0.46, "delta": -0.50, "open_interest": 300, "volume": 30},
        {"type": "call", "strike": 110, "exp_date": "2026-07-15", "dte": 30,
         "volatility": 0.38, "delta": 0.25, "open_interest": 50, "volume": 5},
        {"type": "put", "strike": 90, "exp_date": "2026-07-15", "dte": 30,
         "volatility": 0.50, "delta": -0.25, "open_interest": 200, "volume": 20},
        # дальняя экспирация (dte~90) — выше ATM IV (контанго волы)
        {"type": "call", "strike": 100, "exp_date": "2026-09-15", "dte": 90,
         "volatility": 0.45, "delta": 0.52, "open_interest": 80, "volume": 4},
        {"type": "put", "strike": 100, "exp_date": "2026-09-15", "dte": 90,
         "volatility": 0.47, "delta": -0.48, "open_interest": 120, "volume": 6},
    ]


def test_empty_is_insufficient():
    s = O.summarize([], spot=100)
    assert s["insufficient"] and s["n_contracts"] == 0


def test_core_metrics():
    s = O.summarize(_chain(), spot=100)
    assert s["insufficient"] is False
    assert s["nearest_expiry"] == "2026-07-15"
    # ATM IV ближней = среднее(0.40, 0.46) = 0.43
    assert s["atm_iv"] == 0.43
    # put/call OI = (300+200+120)/(100+50+80) = 620/230
    assert s["put_call_oi_ratio"] == round(620 / 230, 3)
    assert s["total_open_interest"] == 850
    assert s["liquid"] is False            # 850 < 1000


def test_skew_and_term_structure_signs():
    s = O.summarize(_chain(), spot=100)
    # skew: put IV(0.50 @ -0.25) − call IV(0.38 @ 0.25) = 0.12 > 0 (страх)
    assert s["iv_skew_25d_put_minus_call"] == 0.12
    # term: far ATM(avg 0.45,0.47=0.46) − near ATM(0.43) = 0.03 > 0 (контанго волы)
    assert s["iv_term_far_minus_near"] == 0.03


def test_handles_missing_fields():
    chain = [{"type": "call", "strike": 100, "exp_date": "2026-07-15"},
             {"type": "put", "strike": 100, "exp_date": "2026-07-15", "open_interest": 5}]
    s = O.summarize(chain, spot=100)
    assert s["n_contracts"] == 2
    assert s["atm_iv"] is None             # нет volatility → честно None (П8)
