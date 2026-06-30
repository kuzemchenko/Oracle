# -*- coding: utf-8 -*-
"""mathlib/tailprob.py — хвостовые вероятности под ТЯЖЕЛОХВОСТЫМ нулём (Стьюдент-t).

F2#19 (§2.4): event_scan гнал z-аномалии цены/объёма через нормальный erfc(|z|/√2). Доходности
тяжелохвостые, а сырой объём кратно скошен → нормаль резко занижает p → «почти всё проходит FDR».
Двусторонний p под t(df) с малым df даёт ЧЕСТНО больший p в хвостах. Реализация на чистом math
(в проекте нет scipy в рантайме — статистика руками, как _fisher_ci), с тестами против эталонов.
"""
import math


def _betacf(a, b, x):
    """Непрерывная дробь для неполной бета-функции (Lentz), как в Numerical Recipes."""
    MAXIT, EPS, FPMIN = 300, 1e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def betainc(a, b, x):
    """Регуляризованная неполная бета-функция I_x(a, b) ∈ [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_two_sided_p(t, df):
    """Двусторонний p = P(|T_df| ≥ |t|) = I_{df/(df+t²)}(df/2, 1/2). df→∞ сходится к нормали."""
    if df <= 0:
        raise ValueError("df должно быть > 0")
    t2 = float(t) * float(t)
    x = df / (df + t2)
    return max(0.0, min(1.0, betainc(df / 2.0, 0.5, x)))
