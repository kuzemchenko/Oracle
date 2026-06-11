# -*- coding: utf-8 -*-
"""mathlib/calibration/costs.py — издержки по инструментам ядра (§23.1 п.8, §7 «Асимметрия net»).

§23.1 п.8: «исторические спреды, комиссии, проскальзывание — для честного net-матожидания».
Что МОЖНО посчитать из имеющихся дневных OHLCV — считаем; чего НЕТ в данных — честно
помечаем допущением (П8, инвариант 1). Тариф EODHD даёт дневные бары, НЕ котировки bid/ask,
поэтому истинный спред измерить нельзя — он оценивается по ликвидности (медианный $-оборот)
и помечается provenance=assumed. Стоимость заёмных бумаг для шорта в данных отсутствует → «нет данных».

Модель round-trip (в б.п. от нотинала), всё на одной стороне входа+выхода:
    half_spread_bps * 2   (вход и выход пересекают полспреда)  [assumed по тиру ликвидности]
  + slippage_bps * 2       (проскальзывание по участию в обороте) [model]
  + commission_bps * 2     [assumed, тариф брокера]
"""
import numpy as np

# Тиры половины спреда по медианному дневному $-обороту (assumed, типичные ETF/equity).
# Источник чисел — публичные ориентиры спредов ликвидных ETF; provenance=assumed, не из данных.
SPREAD_TIERS = [
    (500e6, 1.0),    # ADV > $500M  → ~1 бп half-spread (SPY-класс)
    (100e6, 2.0),    # > $100M      → ~2 бп
    (25e6, 4.0),     # > $25M       → ~4 бп
    (5e6, 8.0),      # > $5M        → ~8 бп
    (0.0, 20.0),     # тонкий рынок → ~20 бп (по §14 такие к торговле не допускаются)
]

DEFAULT_COMMISSION_BPS = 1.0   # assumed: ~$0.005/share на ~$50 цене ≈ 1 бп; уточняется тарифом
DEFAULT_CAPITAL = 100_000.0    # §30 п.3 капитал-ориентир
DEFAULT_IDEA_FRACTION = 0.005  # §30 п.3 микроразмер этапа Д = 0.5%/идея


def adv_usd(series, window=60):
    """Медианный дневной долларовый оборот за последние `window` дней (close*volume)."""
    n = len(series)
    if n == 0:
        return float("nan")
    k = min(window, n)
    dollar = series.close[-k:] * series.volume[-k:]
    dollar = dollar[np.isfinite(dollar) & (dollar > 0)]
    return float(np.median(dollar)) if dollar.size else float("nan")


def half_spread_bps(adv):
    if not np.isfinite(adv):
        return SPREAD_TIERS[-1][1]
    for thr, bps in SPREAD_TIERS:
        if adv >= thr:
            return bps
    return SPREAD_TIERS[-1][1]


def slippage_bps(order_usd, adv, k=10.0):
    """Проскальзывание (model): k б.п. на каждый 1% участия в дневном обороте.

    participation_pct = 100 * order_usd / adv. Линейная импакт-модель:
        slippage_bps = k * participation_pct.
    При нашем размере ($500/идея) участие микроскопично → проскальзывание ≈ 0,
    но модель честно масштабируется, если размер позиции вырастет.
    """
    if not np.isfinite(adv) or adv <= 0:
        return float("nan")
    participation_pct = 100.0 * order_usd / adv
    return float(k * participation_pct)


def instrument_costs(series, order_usd, window=60,
                     commission_bps=DEFAULT_COMMISSION_BPS):
    """Полная модель издержек одного инструмента. round_trip_bps — двусторонняя сумма."""
    adv = adv_usd(series, window)
    hs = half_spread_bps(adv)
    slp = slippage_bps(order_usd, adv)
    participation = (order_usd / adv) if (np.isfinite(adv) and adv > 0) else float("nan")
    one_way = hs + (slp if np.isfinite(slp) else 0.0) + commission_bps
    return {
        "symbol": series.symbol,
        "adv_usd_median": None if not np.isfinite(adv) else round(adv, 0),
        "adv_window_days": window,
        "order_usd": order_usd,
        "participation_pct": None if not np.isfinite(participation) else round(participation * 100, 6),
        "half_spread_bps": round(hs, 3),
        "half_spread_provenance": "assumed (тир по ADV; дневные бары не содержат bid/ask)",
        "slippage_bps": None if not np.isfinite(slp) else round(slp, 4),
        "slippage_provenance": "model (линейный импакт k=10бп/участие)",
        "commission_bps": round(commission_bps, 3),
        "commission_provenance": "assumed (тариф брокера)",
        "round_trip_bps": round(2 * one_way, 3),
        "short_borrow_fee_bps": None,
        "short_borrow_provenance": "нет данных (ставка заёмных бумаг отсутствует в фиде) — П8",
    }
