# -*- coding: utf-8 -*-
"""mathlib — детерминированное математическое ядро «Оракула» (MASTER_SPEC §21, инвариант 6 CLAUDE.md).

ВСЁ, что можно посчитать, считается здесь КОДОМ с тестами, а НЕ «оценивается» LLM:
Brier и калибровка по корзинам, FDR Бенджамини–Хохберга, запечатывание (timestamp+hash),
сверка исходов, индикаторы теханализа, дробный Келли с shrinkage, программная проверка лимитов.

Единственный санкционированный писатель journal/predictions.jsonl — функция seal()
(только append, П16, инвариант 3). Прямая запись в журнал запрещена хуком guard_journal.py.
"""
from .sealing import (
    seal,
    validate_resolvable,
    is_resolvable,
    read_predictions,
    verify_seal,
    verify_all,
    now_utc_iso,
    PREDICTIONS_PATH,
)
from .brier import brier_score, calibration_table, calibration_band_pp, reliability
from .fdr import benjamini_hochberg
from .outcomes import resolve_prediction, reconcile_journal, to_brier_inputs
from .indicators import (
    returns,
    log_returns,
    sma,
    ema,
    rolling_std,
    realized_vol,
    zscore,
    rsi,
    atr,
    bollinger,
    macd,
    max_drawdown,
)
from .waves import (
    zigzag_pivots,
    fib_retracement,
    nearest_fib,
    label_impulse,
    label_correction,
    wave_markup,
)
from .kelly import shrink_probability, kelly_fraction, position_size
from .limits import (
    load_limits,
    check_idea_risk,
    check_monthly_risk,
    check_fast_basket,
    check_monthly_budget,
    check_run_token_budget,
)
from .masked_eval import score_case, aggregate, GATE_FRACTION

__all__ = [
    # sealing (§9, П16)
    "seal", "validate_resolvable", "is_resolvable", "read_predictions",
    "verify_seal", "verify_all", "now_utc_iso", "PREDICTIONS_PATH",
    # brier / калибровка (П7, §10.9)
    "brier_score", "calibration_table", "calibration_band_pp", "reliability",
    # FDR (§6, §23.1 п.6)
    "benjamini_hochberg",
    # сверка исходов (§10.10)
    "resolve_prediction", "reconcile_journal", "to_brier_inputs",
    # индикаторы (§4, §23.1 п.1)
    "returns", "log_returns", "sma", "ema", "rolling_std", "realized_vol",
    "zscore", "rsi", "atr", "bollinger", "macd", "max_drawdown",
    # разметка волн Эллиотта (§4 «Волновик», §21)
    "zigzag_pivots", "fib_retracement", "nearest_fib", "label_impulse",
    "label_correction", "wave_markup",
    # Келли (§4 портфель, §11)
    "shrink_probability", "kelly_fraction", "position_size",
    # лимиты (§11, §12, инвариант 5)
    "load_limits", "check_idea_risk", "check_monthly_risk", "check_fast_basket",
    "check_monthly_budget", "check_run_token_budget",
    # оценка маскированных кейсов (§23.2б, Нед.8)
    "score_case", "aggregate", "GATE_FRACTION",
]
