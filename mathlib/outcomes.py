# -*- coding: utf-8 -*-
"""mathlib/outcomes.py — детерминированная сверка исходов прогнозов (MASTER_SPEC §10.10, §4 «Разборщик»).

Берёт запечатанный прогноз (§9: direction ∈ {above,below}, threshold, resolve_by, price_source)
и фактическое наблюдение цены → выносит исход 0/1 ЧИСТЫМ кодом (§21), не LLM.
Честность (П8): пока срок не наступил или нет данных — статус 'pending', исход НЕ выдумывается.

Сравнение времён лексикографическое — корректно для ISO 8601 UTC одинакового формата
(весь проект пишет время в UTC, см. data/news_common.now_utc_iso / parse_*).
"""


def resolve_prediction(pred, observed_value, observed_at):
    """Сверка одного прогноза с фактом.

    pred           — dict с полями direction ('above'|'below'), threshold (число), resolve_by (ISO),
                     опционально probability (для последующего Brier).
    observed_value — значение от price_source на/после resolve_by (None → нет данных).
    observed_at    — ISO-время наблюдения (None → нет данных).

    Возвращает dict: status ('pending'|'resolved'|'error'), outcome (1|0|None), probability и контекст.
    """
    resolve_by = str(pred.get("resolve_by", ""))
    direction = str(pred.get("direction", "")).strip().lower()
    res = {
        "asset": pred.get("asset"),
        "direction": direction,
        "threshold": pred.get("threshold"),
        "resolve_by": resolve_by,
        "probability": pred.get("probability"),
        "observed_value": observed_value,
        "observed_at": observed_at,
        "status": "pending",
        "outcome": None,
    }
    if observed_value is None or observed_at is None:
        return res                              # нет данных — pending (П8)
    if str(observed_at) < resolve_by:
        return res                              # срок ещё не наступил
    try:
        thr = float(pred["threshold"])
        val = float(observed_value)
    except (TypeError, ValueError, KeyError):
        res["status"] = "error"
        res["error"] = "threshold/observed_value не число"
        return res
    if direction == "above":
        hit = val >= thr
    elif direction == "below":
        hit = val <= thr
    else:
        res["status"] = "error"
        res["error"] = f"неизвестное direction {direction!r}"
        return res
    res["status"] = "resolved"
    res["outcome"] = 1 if hit else 0
    return res


def reconcile_journal(items):
    """Пакетная сверка. items — список троек (pred, observed_value, observed_at).
    Возвращает список resolved-диктов в том же порядке."""
    return [resolve_prediction(p, v, a) for (p, v, a) in items]


def to_brier_inputs(resolved):
    """Из resolved-диктов собрать (probs, outcomes) ТОЛЬКО по разрешённым записям с probability.
    pending / без probability / error — пропускаются (в Brier попадает только сверенное)."""
    probs, outs = [], []
    for r in resolved:
        if r.get("status") == "resolved" and r.get("outcome") is not None and r.get("probability") is not None:
            probs.append(float(r["probability"]))
            outs.append(int(r["outcome"]))
    return probs, outs
