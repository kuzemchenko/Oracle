# -*- coding: utf-8 -*-
"""mathlib/base_odds.py — БАЗА «случайного взгляда» для §9-вопроса «закроется выше/ниже порога к дате».

Перепроверка №2 архитектуры охоты (03.11): «счёт метода охотника» печатал голое «сбылось K из N» —
без базы такая доля ничего не говорит (порог в 3% от цены сбывается чаще, чем в 20%). База — шанс,
что бумага закроется по ту сторону порога ПРОСТО от своей обычной качки, без всякого взгляда:
безсдвиговое логнормальное блуждание с дневной σ из последних закрытий (Инв#6 — код, не LLM).
Метод полезен, только если сбывается чаще этой базы на тех же порогах; сравнение ведёт счёт метода.

Ограничения (честно): хвосты тяжелее нормальных, праздники не вычтены (рабочие дни пн–пт),
σ — историческая, не опционная. Это мерка «чего ждать от шума», а не прогноз.
"""
import datetime
import math


def _phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def daily_sigma(closes, window=60):
    """Дневная σ логдоходностей по последним `window` закрытиям (ddof=1). Мало данных → None."""
    xs = [float(c) for c in (closes or []) if c is not None and float(c) > 0][-(window + 1):]
    if len(xs) < 21:
        return None
    r = [math.log(b / a) for a, b in zip(xs, xs[1:])]
    m = sum(r) / len(r)
    var = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var) if var > 0 else None


def trading_days(start, end):
    """Число рабочих дней (пн–пт) в (start, end] — горизонт до даты сверки."""
    if isinstance(start, str):
        start = datetime.date.fromisoformat(start[:10])
    if isinstance(end, str):
        end = datetime.date.fromisoformat(end[:10])
    n, d = 0, start
    while d < end:
        d += datetime.timedelta(days=1)
        if d.isoweekday() <= 5:
            n += 1
    return n


def base_probability(price, threshold, sigma_daily, days, direction):
    """P(закрытие через `days` торговых дней по ту сторону порога) под безсдвиговым логнормальным
    блужданием. direction: 'above' | 'below'. Некорректный ввод → None (П8: не выдумываем)."""
    try:
        price, threshold, sigma_daily = float(price), float(threshold), float(sigma_daily)
    except (TypeError, ValueError):
        return None
    if price <= 0 or threshold <= 0 or sigma_daily <= 0 or not days or days <= 0:
        return None
    s = sigma_daily * math.sqrt(days)
    z = math.log(threshold / price) / s
    p_above = 1.0 - _phi(z)
    if direction == "above":
        return p_above
    if direction == "below":
        return 1.0 - p_above
    return None


def poisson_binomial_sf(k, probs):
    """P(X ≥ k), X = сумма независимых Бернулли с вероятностями probs (точная динамика, Инв#6).
    Значимость «метод сбывается чаще шума»: k попаданий против баз шума тех же вопросов."""
    ps = [float(p) for p in (probs or [])]
    if any(not 0.0 <= p <= 1.0 for p in ps):
        return None
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for j, q in enumerate(dist):
            nxt[j] += q * (1.0 - p)
            nxt[j + 1] += q * p
        dist = nxt
    k = max(0, int(k))
    return float(min(1.0, sum(dist[k:]))) if k < len(dist) else 0.0
