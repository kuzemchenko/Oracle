# -*- coding: utf-8 -*-
"""mathlib/tectonic.py — оценка ТЕКТОНИЧЕСКОГО ПОТЕНЦИАЛА события/каскада (пилот, §5/П5).

Детерминированная математика (§21 «всё, что можно посчитать — считается»): по карте цепочки
(knowledge/cascade_chains.yaml) считает потенциал каскада и выбирает НАИМЕНЕЕ ОТЫГРАННЫЙ
торгуемый дальний узел-чокпоинт — то, ради чего система и существует (неочевидное дальнее звено).

Оси рубрики (см. диалог проектирования):
  M — магнитуда первичного шока        P — персистентность (режимный сдвиг vs разовый репрайс)
  C — связность/центральность (фан-аут, доля чокпоинтов)   S — сюрприз/неконсенсус
  L — окно входа (сумма лагов переноса) A — асимметрия дальнего рычажного узла
Итог T = взвешенная сумма; entry_score = T × (1 − отыгранность выбранного узла).

LLM-судимые оси (M/P/S) приходят строками-уровнями из карты (low/medium/high/structural…) или
переопределяются вызывающим; математические оси (C/L/A) считаются из структуры графа. Никаких
выдумок: нет данных по оси → нейтральное 0.5 и пометка в notes (П8).
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAINS = ROOT / "knowledge" / "cascade_chains.yaml"

# Уровни → числа (LLM/доменные оси). Нейтраль 0.5 при отсутствии.
_MAGNITUDE = {"low": 0.3, "medium": 0.6, "high": 1.0}
_PERSISTENCE = {"transient": 0.3, "multi_quarter": 0.7, "structural": 1.0}
_SURPRISE = {"low": 0.3, "medium": 0.6, "high": 1.0}
_PRICED = {"low": 0.2, "medium": 0.5, "high": 0.85}

# Веса осей тектонического потенциала (сумма = 1.0).
_W = {"M": 0.20, "C": 0.25, "P": 0.20, "S": 0.10, "L": 0.15, "A": 0.10}

_LAG_FULL_WINDOW_DAYS = 180  # лаг ≥ полугода → максимум окна входа (L=1.0)


def load_chains(path=CHAINS):
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("chains", [])


def get_chain(chain_id, path=CHAINS):
    for c in load_chains(path):
        if c.get("id") == chain_id:
            return c
    return None


def _level(mapping, value, default=0.5):
    if value is None:
        return default, True   # (значение, was_missing)
    return mapping.get(str(value).lower(), default), str(value).lower() not in mapping


def _connectivity(nodes):
    """C: доля узлов-чокпоинтов + ширина торгуемого фан-аута (число различных инструментов)."""
    if not nodes:
        return 0.0
    choke = sum(1 for n in nodes if n.get("chokepoint"))
    instruments = {i for n in nodes for i in (n.get("instruments") or [])}
    choke_frac = choke / len(nodes)
    breadth = min(len(instruments) / 6.0, 1.0)
    return round(0.5 * choke_frac + 0.5 * breadth, 4)


def _lag_window(edges):
    """L: суммарный лаг переноса → окно входа (медленные деньги; больше лаг = больше времени)."""
    total = sum((e.get("lag_days") or 0) for e in (edges or []))
    return round(min(total / _LAG_FULL_WINDOW_DAYS, 1.0), 4), total


def _best_far_node(nodes, priced_override=None):
    """Наименее отыгранный ТОРГУЕМЫЙ дальний узел; приоритет — чокпоинт максимального порядка."""
    tradable = [n for n in nodes if (n.get("instruments"))]
    if not tradable:
        return None
    def priced_of(n):
        if priced_override and n.get("order") in priced_override:
            return priced_override[n["order"]]
        return _PRICED.get(str(n.get("priced_hint")).lower(), 0.5)
    # сортировка: сначала чокпоинты, потом больший порядок, потом меньшая отыгранность
    tradable.sort(key=lambda n: (n.get("chokepoint", False), n.get("order", 0), -priced_of(n)),
                  reverse=True)
    best = tradable[0]
    return {"order": best.get("order"), "node": best.get("node"),
            "instruments": best.get("instruments"), "chokepoint": bool(best.get("chokepoint")),
            "priced": round(priced_of(best), 4)}


def score_chain(chain, *, magnitude=None, persistence=None, surprise=None, priced_override=None):
    """Оценка тектонического потенциала цепочки. Переопределения (LLM-суждение) важнее карты.

    Возвращает {components, tectonic_potential, lag_window_days, best_far_node, entry_score, notes}.
    """
    trig = chain.get("trigger") or {}
    nodes = chain.get("nodes") or []
    notes = []

    M, m_miss = _level(_MAGNITUDE, magnitude or trig.get("magnitude"))
    P, p_miss = _level(_PERSISTENCE, persistence or trig.get("persistence"))
    S, s_miss = _level(_SURPRISE, surprise or trig.get("surprise"))
    for nm, miss in (("магнитуда", m_miss), ("персистентность", p_miss), ("сюрприз", s_miss)):
        if miss:
            notes.append(f"ось {nm}: нет уровня в карте → нейтраль 0.5 (П8)")
    C = _connectivity(nodes)
    L, total_lag = _lag_window(chain.get("edges"))
    far = _best_far_node(nodes, priced_override)
    # A: есть ли торгуемый чокпоинт глубокого порядка (рычажное дальнее звено)
    A = 0.85 if (far and far["chokepoint"] and far["order"] >= 3) else (0.6 if far else 0.4)

    comps = {"M": round(M, 4), "C": C, "P": round(P, 4), "S": round(S, 4),
             "L": L, "A": round(A, 4)}
    T = round(sum(_W[k] * comps[k] for k in _W), 4)
    entry = round(T * (1.0 - (far["priced"] if far else 0.5)), 4)
    return {
        "chain_id": chain.get("id"),
        "components": comps,
        "weights": _W,
        "tectonic_potential": T,           # «насколько большой каскад» (0..1)
        "lag_window_days": total_lag,      # суммарное окно входа по лагам
        "best_far_node": far,              # куда целиться: неотыгранный дальний чокпоинт
        "entry_score": entry,              # потенциал × неотыгранность узла
        "notes": notes,
    }


def rank_chains(path=CHAINS, **overrides):
    """Ранжировать все цепочки по entry_score (для этапа 1 мульти-событийного режима)."""
    scored = [score_chain(c, **overrides) for c in load_chains(path)]
    scored.sort(key=lambda s: s["entry_score"], reverse=True)
    return scored
