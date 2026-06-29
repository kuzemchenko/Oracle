# -*- coding: utf-8 -*-
"""mathlib/calibration/forward_promotion.py — ФОРВАРД-промоушен каскадных рёбер в ярус A.

Решение владельца 28.06.2026 (см. memory forward-promotion-edge-c-to-a): ребро каскада
зарабатывает право на ярус-A (а значит money-трек) не 3 годами исторической беты (что недостижимо
на молодых листингах — calibrate_pair_sensitivity требует 756 торг.дней), а КОРРЕКТНЫМИ
ЗАПЕЧАТАННЫМИ ФОРВАРД-ПРОГНОЗАМИ. Это самый честный путь по П16: ребро доказывает перенос на
будущем, а не подгоняется на прошлом.

Критерий — СТРОГИЙ §10 (одобрен владельцем), скилл НАД БАЗОВОЙ СТАВКОЙ (F0#5, не над монеткой 0.5):
  • N ≥ MIN_OUTCOMES (=30) разрешённых форвард-исходов по ребру;
  • направленный hit-rate значимо > p0 = max(base_rate, 1−base_rate) — hit-rate тривиального
    предиктора «всегда мажоритарное направление» (односторонний ТОЧНЫЙ биномтест, α=ALPHA);
  • Brier Skill Score над климатологией base_rate·(1−base_rate) > MIN_BSS (=0.05).
Иначе НЕ промоутим — ребро остаётся провизорным/research (П8: нет доказательства переноса).

Атрибуция: исход относится к РЕБРУ только если путь прогноза однозвенный (len(path_edges)==1) —
тогда бинарный исход терминала чисто измеряет это ребро. Многозвенные (order 3+) пути НЕ
атрибутируются одному ребру (их сигнал композитный) — они становятся money лишь когда КАЖДОЕ их
ребро промоутировано отдельно (all_A в compose_chain).

Только детерминированный код (инвариант #6). LLM здесь нет.
"""
import math

MIN_OUTCOMES = 30        # §10: N≥30 разрешённых исходов на ребро до промоушена
ALPHA = 0.05             # уровень значимости одностороннего биномтеста hit_rate > base-rate
MIN_BSS = 0.05           # F0#5: минимальный Brier Skill Score НАД климатологией (base-rate), а не над 0.5
EPS = 1e-9               # порог «нет направленного вызова» (p ровно 0.5)
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
    """Скилл ребра по запечатанным форвард-исходам — НАД БАЗОВОЙ СТАВКОЙ (climatology), не над монеткой.
    probs/outcomes — направленная вероятность P[исход=1] и бинарный исход 0/1, выровненные по индексу.

    F0#5: раньше тест шёл против фикс. p=0.5 и Brier<0.25 — это пропускало base-rate-монетку (терминал
    с base-rate 0.7 + константный p≈0.7 даёт hit-rate значим против 0.5 и Brier≈0.17<0.25 при НУЛЕВОМ
    условном скилле). Теперь:
      • биномтест направленного hit-rate против p0 = max(base_rate, 1−base_rate) (hit-rate тривиального
        предиктора «всегда мажоритарное направление») — модель должна ПРЕВЗОЙТИ базу, не монетку;
      • Brier Skill Score над климатологией base_rate·(1−base_rate) > MIN_BSS.
    Направленный вызов — только при |p−0.5|>EPS (p ровно 0.5 = нет направления, в hit-тест не идёт)."""
    n = len(outcomes)
    if n == 0 or len(probs) != n:
        return {"n": n, "n_directional": 0, "base_rate": None, "brier": None,
                "climatology_brier": None, "bss": None, "hit_rate": None, "hits": 0,
                "p0": None, "p_value": None, "skill_significant": False,
                "причина": "нет данных (П8): нет выровненных форвард-исходов"}
    ys = [int(y) for y in outcomes]
    base_rate = sum(ys) / n
    p0 = max(base_rate, 1.0 - base_rate)                    # hit-rate тривиального base-rate-предиктора
    # направленные вызовы (исключаем p ровно 0.5 — нет направления, F0#5/§2.7 «p==0.5 как вверх»)
    dir_pairs = [(float(p), y) for p, y in zip(probs, ys) if abs(float(p) - 0.5) > EPS]
    n_dir = len(dir_pairs)
    hits = sum(1 for p, y in dir_pairs if (p > 0.5) == (y == 1))
    hit_rate = (hits / n_dir) if n_dir else None
    brier = sum((float(p) - y) ** 2 for p, y in zip(probs, ys)) / n
    clim_brier = base_rate * (1.0 - base_rate)             # Brier предсказания base_rate каждый раз
    bss = (1.0 - brier / clim_brier) if clim_brier > 0 else None   # вырожденная база (все исходы равны) → скилл недоказуем
    p_value = _binom_sf_ge(hits, n_dir, p0) if n_dir else None
    significant = bool(p_value is not None and p_value <= ALPHA
                       and bss is not None and bss > MIN_BSS)
    return {"n": n, "n_directional": n_dir, "base_rate": round(base_rate, 4),
            "brier": round(brier, 6), "climatology_brier": round(clim_brier, 6),
            "bss": (round(bss, 4) if bss is not None else None),
            "hit_rate": (round(hit_rate, 4) if hit_rate is not None else None),
            "hits": hits, "p0": round(p0, 4),
            "p_value": (round(p_value, 6) if p_value is not None else None),
            "skill_significant": significant}


def reliability_from_skill(bss):
    """Надёжность ребра из Brier Skill Score над климатологией (F0#5: скилл НАД базой, не над монеткой).
    bss≤0 → 0; растёт с реальным скиллом до потолка FORWARD_RELIABILITY_CAP (N мал — не идеальная уверенность)."""
    if bss is None:
        return 0.0
    return round(min(max(0.0, float(bss)), FORWARD_RELIABILITY_CAP), 4)


def promote_decision(probs, outcomes, *, beta_fullsample=None, min_outcomes=MIN_OUTCOMES):
    """Полный вердикт по ребру: промоутить ли в ярус A. Промоушен только при N≥min_outcomes И значимом
    скилле НАД базовой ставкой. beta_fullsample — точечная бета (форвард доказывает ПЕРЕНОС, не величину)."""
    sk = forward_skill(probs, outcomes)
    n = sk["n"]
    enough = n >= min_outcomes
    promote = bool(enough and sk["skill_significant"])
    rec = dict(sk)
    rec["enough_outcomes"] = enough
    rec["min_outcomes"] = min_outcomes
    rec["promote"] = promote
    rec["reliability"] = reliability_from_skill(sk.get("bss")) if promote else 0.0
    rec["beta_fullsample"] = beta_fullsample
    if promote:
        rec["причина"] = (f"ПРОМОУШЕН→ярус A: N={n}≥{min_outcomes}, hit-rate {sk['hit_rate']:.0%} > "
                          f"база {sk['p0']:.0%} (p={sk['p_value']}≤{ALPHA}), BSS {sk['bss']}>{MIN_BSS}")
    else:
        why = []
        if not enough:
            why.append(f"исходов {n}<{min_outcomes} (§10)")
        if not sk["skill_significant"] and n:
            why.append(f"скилл над базой незначим (p={sk.get('p_value')}, BSS={sk.get('bss')}, "
                       f"base_rate={sk.get('base_rate')})")
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
