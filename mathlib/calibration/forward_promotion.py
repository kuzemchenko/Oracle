# -*- coding: utf-8 -*-
"""mathlib/calibration/forward_promotion.py — ФОРВАРД-промоушен каскадных рёбер в ярус A.

Решение владельца 28.06.2026 (см. memory forward-promotion-edge-c-to-a): ребро каскада
зарабатывает право на ярус-A (а значит money-трек) не 3 годами исторической беты (что недостижимо
на молодых листингах — calibrate_pair_sensitivity требует 756 торг.дней), а КОРРЕКТНЫМИ
ЗАПЕЧАТАННЫМИ ФОРВАРД-ПРОГНОЗАМИ. Это самый честный путь по П16: ребро доказывает перенос на
будущем, а не подгоняется на прошлом.

Критерий — СТРОГИЙ §10 (одобрен владельцем):
  • N ≥ MIN_OUTCOMES (=30) разрешённых форвард-исходов по ребру;
  • направленный hit-rate значимо > 0.5 (односторонний ТОЧНЫЙ биномтест, α=ALPHA);
  • форвард-Brier < BASE_BRIER (0.25 — «монетка» при p=0.5).
Иначе НЕ промоутим — ребро остаётся провизорным/research (П8: нет доказательства переноса).

Атрибуция: исход относится к РЕБРУ только если путь прогноза однозвенный (len(path_edges)==1) —
тогда бинарный исход терминала чисто измеряет это ребро. Многозвенные (order 3+) пути НЕ
атрибутируются одному ребру (их сигнал композитный) — они становятся money лишь когда КАЖДОЕ их
ребро промоутировано отдельно (all_A в compose_chain).

Только детерминированный код (инвариант #6). LLM здесь нет.
"""
import math

MIN_OUTCOMES = 30        # §10: N≥30 разрешённых исходов на ребро до промоушена
ALPHA = 0.05             # уровень значимости одностороннего биномтеста hit_rate>0.5
BASE_BRIER = 0.25        # Brier неинформативного p=0.5; форвард-Brier должен быть строго ниже
FORWARD_RELIABILITY_CAP = 0.7   # потолок надёжности заработанной форвардом (между structural 0.6 и пином)


def edge_key(up, down, lag=0):
    """Каноничный ключ ребра для атрибуции форвард-исходов и поиска промоушена. Лаг входит в ключ —
    разный лаг переноса = разное ребро (как в калибровке чувствительностей)."""
    return f"{up}->{down}@lag{int(lag or 0)}"


def _binom_sf_ge(k, n, p=0.5):
    """P(X ≥ k) при X~Binom(n,p) — точно (math.comb). Односторонний p-value для hit_rate>0.5."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) * p**i * (1.0 - p)**(n - i) for i in range(k, n + 1))


def forward_skill(probs, outcomes):
    """Скилл ребра по запечатанным форвард-исходам. probs/outcomes — направленная вероятность
    (P[исход=1]) и бинарный исход 0/1, выровненные по индексу. Возвращает метрики и вердикт-готовность
    (без применения порога N — это решает promote_decision). hit = (p≥0.5)==(outcome==1)."""
    n = len(outcomes)
    if n == 0 or len(probs) != n:
        return {"n": n, "brier": None, "hit_rate": None, "hits": 0,
                "p_value": None, "skill_significant": False,
                "причина": "нет данных (П8): нет выровненных форвард-исходов"}
    hits = sum(1 for p, y in zip(probs, outcomes) if (p >= 0.5) == (int(y) == 1))
    hit_rate = hits / n
    brier = sum((float(p) - int(y)) ** 2 for p, y in zip(probs, outcomes)) / n
    p_value = _binom_sf_ge(hits, n, 0.5)
    significant = (p_value <= ALPHA) and (brier < BASE_BRIER)
    return {"n": n, "brier": round(brier, 6), "hit_rate": round(hit_rate, 4),
            "hits": hits, "p_value": round(p_value, 6),
            "skill_significant": bool(significant)}


def reliability_from_skill(hit_rate):
    """Надёжность ребра из направленного hit-rate: 0.5→0, линейно до потолка. Заработана форвардом,
    но потолок FORWARD_RELIABILITY_CAP (N=30 — небольшая выборка, не выдаём идеальную уверенность)."""
    if hit_rate is None:
        return 0.0
    rel = max(0.0, 2.0 * (float(hit_rate) - 0.5))
    return round(min(rel, FORWARD_RELIABILITY_CAP), 4)


def promote_decision(probs, outcomes, *, beta_fullsample=None, min_outcomes=MIN_OUTCOMES):
    """Полный вердикт по ребру: промоутить ли в ярус A. Возвращает запись (промоутится только при
    N≥min_outcomes И значимом скилле). beta_fullsample — точечная бета (направление/масштаб остаётся
    исторической оценкой; форвард доказывает ПЕРЕНОС, не калибрует величину)."""
    sk = forward_skill(probs, outcomes)
    n = sk["n"]
    enough = n >= min_outcomes
    promote = bool(enough and sk["skill_significant"])
    rec = dict(sk)
    rec["enough_outcomes"] = enough
    rec["min_outcomes"] = min_outcomes
    rec["promote"] = promote
    rec["reliability"] = reliability_from_skill(sk.get("hit_rate")) if promote else 0.0
    rec["beta_fullsample"] = beta_fullsample
    if promote:
        rec["причина"] = (f"ПРОМОУШЕН→ярус A: N={n}≥{min_outcomes}, hit-rate {sk['hit_rate']:.0%} "
                          f"(p={sk['p_value']}≤{ALPHA}), форвард-Brier {sk['brier']}<{BASE_BRIER}")
    else:
        why = []
        if not enough:
            why.append(f"исходов {n}<{min_outcomes} (§10)")
        if not sk["skill_significant"] and n:
            why.append(f"скилл незначим (p={sk.get('p_value')}, Brier={sk.get('brier')})")
        rec["причина"] = "НЕ промоутится (форвард-онли): " + "; ".join(why or ["нет данных (П8)"])
    return rec


def aggregate_by_edge(rows):
    """Группирует форвард-исходы по ребру. rows — список dict с ключами:
    edge_key (str), probability (float|None), outcome (0/1|None), beta_fullsample (опц.).
    Возвращает {edge_key: {"probs":[...], "outcomes":[...], "beta_fullsample": <посл. не-None>}}.
    Отбрасывает строки без вероятности/исхода (П8)."""
    by = {}
    for r in rows:
        key = r.get("edge_key")
        p, y = r.get("probability"), r.get("outcome")
        if not key or p is None or y not in (0, 1, 0.0, 1.0):
            continue
        b = by.setdefault(key, {"probs": [], "outcomes": [], "beta_fullsample": None})
        b["probs"].append(float(p))
        b["outcomes"].append(int(y))
        if r.get("beta_fullsample") is not None:
            b["beta_fullsample"] = float(r["beta_fullsample"])
    return by


def promote_all(rows, *, min_outcomes=MIN_OUTCOMES):
    """rows → {edge_key: promote_decision(...)} по всем рёбрам. Вход промоушен-драйвера."""
    out = {}
    for key, b in aggregate_by_edge(rows).items():
        out[key] = promote_decision(b["probs"], b["outcomes"],
                                    beta_fullsample=b["beta_fullsample"],
                                    min_outcomes=min_outcomes)
    return out
