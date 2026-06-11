# -*- coding: utf-8 -*-
"""mathlib/kelly.py — дробный Келли с shrinkage и размер позиции (MASTER_SPEC §4 портфель, §11).

§4 «Портфельный менеджер»: дробный Келли со СТЯГИВАНИЕМ вероятности к 50% пропорционально
НЕдоказанности калибровки; включается ТОЛЬКО после gate калибровки — до того ФИКС 0.5% капитала/идея.
§21: размер позиции считает КОД, не LLM. Отрицательный Келли (нет края) → 0 (не ставим).
"""


def shrink_probability(p, calibration_proven):
    """Стянуть вероятность к 0.5 пропорционально НЕдоказанности калибровки.
    calibration_proven ∈ [0,1]: 1 — калибровка доказана (без стягивания), 0 — не доказана (p→0.5).
    p_eff = 0.5 + (p - 0.5) * proven."""
    if not 0.0 <= p <= 1.0:
        raise ValueError("p должно быть в [0,1]")
    proven = max(0.0, min(1.0, float(calibration_proven)))
    return 0.5 + (p - 0.5) * proven


def kelly_fraction(p, b):
    """Полная доля Келли для ставки с net-оддсами b (выигрываем b на 1 поставленную при успехе):
    f* = (b*p - (1-p)) / b. Отрицательная (нет края) обрезается до 0 — не ставим."""
    if not 0.0 <= p <= 1.0:
        raise ValueError("p должно быть в [0,1]")
    if b <= 0:
        raise ValueError("b (net-оддсы) должны быть > 0")
    f = (b * p - (1.0 - p)) / b
    return max(0.0, f)


def position_size(p, b, capital, *, calibration_proven=0.0, kelly_multiplier=0.5,
                  gate_passed=False, microsize_pct=0.5, max_pct=None):
    """Размер позиции в деньгах.

    gate_passed=False (по умолчанию, §11 этап Д до подтверждения калибровки):
        ФИКС microsize_pct% капитала/идея — Келли НЕ применяется.
    gate_passed=True:
        дробный Келли (kelly_multiplier ∈ [0,1]) от СТЯНУТОЙ вероятности; опционально потолок max_pct%.

    Возвращает dict с разбивкой (method, fraction, amount_usd, ...).
    """
    if capital <= 0:
        raise ValueError("capital должен быть > 0")
    if not gate_passed:
        frac = microsize_pct / 100.0
        return {"method": "fixed_microsize", "fraction": frac,
                "amount_usd": capital * frac, "p_used": None, "gate_passed": False}
    p_eff = shrink_probability(p, calibration_proven)
    f_full = kelly_fraction(p_eff, b)
    f = f_full * max(0.0, min(1.0, kelly_multiplier))
    if max_pct is not None:
        f = min(f, max_pct / 100.0)
    return {"method": "fractional_kelly", "fraction": f, "amount_usd": capital * f,
            "p_used": p_eff, "kelly_full": f_full, "gate_passed": True}
