# -*- coding: utf-8 -*-
"""mathlib/base_rate.py — ДЕТЕРМИНИРОВАННАЯ эмпирическая базовая ставка идеи (Инв#6, П8).

Базовая ставка = безусловная историческая частота, что цена за горизонт h двинулась в сторону
`direction` на ≥ k·σ_h. Это якорь судье/прогнозу, ИЗМЕРЕННЫЙ по истории, а НЕ выдуманный LLM.

F2#17 (§2.2): раньше base_rate брался из суждения генератора (gen_j['base_rate']) — выдуманное
число, нарушение Инв#6 («математика — не LLM»). Методология зеркалит orchestrator/calibrate.py
(σ_H через realized_vol последних 60 лог-ретёрнов; шкала хода как K_OFFSETS=±0.5σ), но направленно.
"""
import math

from mathlib import indicators as IND

H_DEFAULT = 5        # недельный горизонт §17.3 (= calibrate.H_TRADING_DAYS)
K_DEFAULT = 0.5      # «ход, достойный идеи» — шкала K_OFFSETS калибровки (полусигма за горизонт)
MIN_VOL_OBS = 61     # минимум точек для оценки σ (60 лог-ретёрнов)


def _dir_sign(direction):
    """+1 для лонг/рост, −1 для шорт/падение, 0 — направление не задано/неизвестно (П8)."""
    if direction is None:
        return 0
    low = str(direction).strip().lower()
    if any(t in low for t in ("лонг", "long", "above", "buy", "рост", "выше")):
        return 1
    if any(t in low for t in ("шорт", "short", "below", "sell", "паден", "ниже")):
        return -1
    return 0


def empirical_directional_base_rate(adj_closes, direction, h=H_DEFAULT, k=K_DEFAULT):
    """Частота h-дневного хода ≥ k·σ_h В СТОРОНУ `direction` по истории adj_closes.

    Возвращает (base_rate|None, n_windows). П8: неизвестное направление / мало истории → (None, n).
    σ_h берётся из текущей realized vol (как в calibrate), порог хода log_move = k·σ_h.
    """
    px = [float(p) for p in (adj_closes or []) if p is not None and float(p) > 0]
    sign = _dir_sign(direction)
    if sign == 0 or len(px) < MIN_VOL_OBS + h:
        return None, 0
    sigma_d = float(IND.realized_vol(px[-MIN_VOL_OBS:]))
    sigma_h = sigma_d * math.sqrt(h)
    if not (sigma_h > 0):
        return None, 0
    log_move = k * sigma_h
    hits = tot = 0
    for i in range(len(px) - h):
        r = math.log(px[i + h] / px[i])
        if (sign > 0 and r >= log_move) or (sign < 0 and r <= -log_move):
            hits += 1
        tot += 1
    return (((hits / tot) if tot else None), tot)
