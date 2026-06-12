# -*- coding: utf-8 -*-
"""mathlib/scoring.py — ДЕТЕРМИНИРОВАННЫЙ скоринг идей §7 (MASTER_SPEC §7, инвариант 6 CLAUDE.md).

§7: балл идеи = взвешенная сумма 6 критериев (веса из config/weights.yaml, утверждены §30:
22/22/18/14/14/10). Само взвешивание — МАТЕМАТИКА, не LLM: агенты дают per-критериальные
оценки в [0,1], код их складывает с версионируемыми весами. Это «скоринг §7» блока F.

Критерии (каждый нормализован к [0,1]):
  probability_success   — калиброванная вероятность судьи (от base_rate)
  asymmetry_net         — матожидание после ВСЕХ издержек (helper net_asymmetry_score)
  non_obviousness       — насколько НЕ отыграно (включая вердикт тайминга)
  data_reliability      — качество и credibility источников
  risk_controllability  — хедж/ликвидность/стоп/низкий манип-балл
  competence_proximity  — близость к кругу компетенции §13

«Ниже порога — не показывается» (§7): порог отбора — параметр (по умолчанию 0.0, отбор
делает ранжирование этапа 4, а не жёсткий порог; жёсткий порог можно поднять конфигом).
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEIGHTS_PATH = ROOT / "config" / "weights.yaml"

CRITERIA = ("probability_success", "asymmetry_net", "non_obviousness",
            "data_reliability", "risk_controllability", "competence_proximity")


def load_scoring_weights(path=WEIGHTS_PATH):
    with open(path, encoding="utf-8") as f:
        w = yaml.safe_load(f)
    sc = (w or {}).get("scoring", {})
    missing = [c for c in CRITERIA if c not in sc]
    if missing:
        raise KeyError(f"в weights.yaml нет весов скоринга: {missing}")
    return {c: float(sc[c]) for c in CRITERIA}, w.get("version")


def _clip01(x, name):
    if x is None:
        raise ValueError(f"критерий '{name}' = None: дай оценку или 'нет данных' через score_idea(..., missing=...)")
    x = float(x)
    if not 0.0 <= x <= 1.0:
        raise ValueError(f"критерий '{name}' вне [0,1]: {x}")
    return x


def net_asymmetry_score(prob, round_trip_bps, *, win_move_bps, loss_move_bps,
                        short_borrow_bps=None, horizon_days=10):
    """Оценка асимметрии net в [0,1] из матожидания ПОСЛЕ издержек (§7 «Асимметрия net», §8 п.4).

    EV_bps = prob*win_move - (1-prob)*loss_move - round_trip - borrow*horizon.
    Нормируем логистически в [0,1] относительно масштаба сделки (потенциального выигрыша).
    borrow=None (нет данных для шорта, П8) → считаем 0, но это ЗАНИЖАЕТ издержки шорта —
    флаг возвращается, чтобы вызвавший пометил пробел.
    """
    if not 0.0 <= prob <= 1.0:
        raise ValueError("prob вне [0,1]")
    borrow_total = 0.0 if short_borrow_bps is None else float(short_borrow_bps) * horizon_days
    ev = prob * win_move_bps - (1 - prob) * loss_move_bps - round_trip_bps - borrow_total
    scale = max(1.0, float(win_move_bps))
    # логистика: EV=0 → 0.5; EV=+scale → ~0.73; EV=-scale → ~0.27
    import math
    score = 1.0 / (1.0 + math.exp(-ev / scale))
    return {
        "score": round(score, 4),
        "ev_bps": round(ev, 4),
        "borrow_assumed_zero": short_borrow_bps is None,
        "borrow_note": ("short_borrow_fee_bps=null (нет данных, П8): для шорта истинные издержки "
                        "ЗАНИЖЕНЫ" if short_borrow_bps is None else None),
    }


def score_idea(criteria_values, *, weights=None, version=None, min_score=0.0):
    """Взвешенный балл идеи §7 из словаря критерий→[0,1].

    Возвращает dict: total (взвешенный, [0,1]), breakdown (вклад каждого критерия),
    weights_version, passes (total ≥ min_score), missing (критерии, помеченные 'нет данных').
    Критерий со значением None НЕ допускается — вызывающий обязан подать число ИЛИ передать
    его в missing с консервативной заменой (П8: пробел не превращается тихо в 0 или 1).
    """
    if weights is None:
        weights, version = load_scoring_weights()
    missing = []
    contrib = {}
    total = 0.0
    for c in CRITERIA:
        v = criteria_values.get(c)
        if v is None:
            raise ValueError(f"критерий '{c}' не задан; для честного пробела подай явное число "
                             f"(консервативная замена) и отметь в protocol.missing")
        v = _clip01(v, c)
        if isinstance(criteria_values.get(c + "_missing"), bool) and criteria_values[c + "_missing"]:
            missing.append(c)
        w = weights[c]
        contrib[c] = round(w * v, 6)
        total += w * v
    total = round(total, 6)
    return {
        "total": total,
        "breakdown": contrib,
        "criteria_values": {c: round(float(criteria_values[c]), 4) for c in CRITERIA},
        "weights_version": version,
        "passes": total >= min_score,
        "min_score": min_score,
        "missing": missing,
    }
