# -*- coding: utf-8 -*-
"""mathlib/skill.py — умение прогнозов против базы шума (точка Б, критерий 3; аудит 03.11).

Brier skill против БАЗЫ (вероятность того же события от одной обычной качки цены — mathlib/base_odds),
а не против монетки: «сбылось 55%» ничего не значит, если шум дал бы 55%. Доверительный интервал —
кластерный бутстрэп (кластер = неделя сверки × механизм: исходы одной недели и одного механизма
коррелированы, считать их независимыми — самообман). При N < min_n вердикт честно откладывается.
Только детерминированная арифметика (Инв#6), ГПСЧ с фиксированным зерном.
"""
import random


def _brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def brier_skill_vs_base(probs, bases, outcomes, clusters=None, n_boot=2000, seed=7, min_n=40):
    """→ {n, brier, brier_base, skill, ci95, verdict}. skill = 1 − Brier/Brier_базы (>0 — лучше шума).

    probs/bases ∈ [0,1], outcomes ∈ {0,1}; clusters — метка кластера на исход (None — каждый сам по себе).
    verdict: «данных мало» (N<min_n) / «лучше шума» (CI95 целиком >0) / «хуже шума» (CI95 <0) /
    «не отличить от шума». Пустые/битые входы → n=0, «данных мало»."""
    rows = []
    for i, (p, b, y) in enumerate(zip(probs or [], bases or [], outcomes or [])):
        try:
            p, b, y = float(p), float(b), int(y)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0 and 0.0 <= b <= 1.0) or y not in (0, 1):
            continue
        rows.append((p, b, y, clusters[i] if clusters is not None and i < len(clusters) else i))
    n = len(rows)
    out = {"n": n, "brier": None, "brier_base": None, "skill": None, "ci95": None,
           "verdict": "данных мало"}
    if n == 0:
        return out
    ps, bs, ys = [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]
    bm, bb = _brier(ps, ys), _brier(bs, ys)
    out.update(brier=round(bm, 6), brier_base=round(bb, 6),
               skill=round(1 - bm / bb, 6) if bb > 0 else None)
    if n < min_n or bb <= 0:
        return out
    groups = {}
    for r in rows:
        groups.setdefault(r[3], []).append(r)
    keys = sorted(groups, key=str)
    rng = random.Random(seed)
    sk = []
    for _ in range(n_boot):
        sample = [r for k in (rng.choice(keys) for _ in keys) for r in groups[k]]
        b1 = _brier([r[1] for r in sample], [r[2] for r in sample])
        if b1 > 0:
            sk.append(1 - _brier([r[0] for r in sample], [r[2] for r in sample]) / b1)
    if not sk:
        return out
    sk.sort()
    lo, hi = sk[int(0.025 * (len(sk) - 1))], sk[int(0.975 * (len(sk) - 1))]
    out["ci95"] = [round(lo, 6), round(hi, 6)]
    out["verdict"] = "лучше шума" if lo > 0 else ("хуже шума" if hi < 0 else "не отличить от шума")
    return out
