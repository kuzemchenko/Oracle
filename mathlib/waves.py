# -*- coding: utf-8 -*-
"""mathlib/waves.py — детерминированная числовая разметка волн (MASTER_SPEC §4 «Волновик», §21).

§21/§4: «Волновик включается ТОЛЬКО поверх числовой разметки волн». Размечает КОД, LLM лишь
ИНТЕРПРЕТИРУЕТ. Этот модуль не «угадывает истинный счёт» (счёт по Эллиотту неоднозначен по
природе — П8): он даёт ИЗМЕРИМЫЕ факты — пивоты (ZigZag по фактическим экстремумам), длины
волн, отношения Фибоначчи и проверку ТРЁХ ЖЁСТКИХ правил Эллиотта. Альтернативный счёт и
интерпретация — за агентом-волновиком.

Три жёстких (неотменяемых) правила импульса Эллиотта, которые можно проверить кодом:
  R1: волна 2 не откатывает дальше начала волны 1 (ретрейс < 100%);
  R2: волна 3 не самая короткая из волн 1, 3, 5;
  R3: волна 4 не заходит в ценовую территорию волны 1 (нет перекрытия) — классическое правило;
      в диагоналях перекрытие допускается, поэтому нарушение R3 помечается, но отдельно.
Чистый numpy, без сети и LLM.
"""
import numpy as np

# Стандартные уровни Фибоначчи (ретрейсы и расширения) для привязки измеренных отношений.
FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.382, 1.618, 2.0, 2.618)


def _arr(x):
    a = np.asarray(x, dtype=float)
    if a.ndim != 1:
        raise ValueError("ожидается 1-D ряд")
    return a


def zigzag_pivots(prices, threshold_pct=0.05):
    """ZigZag-пивоты по фактическим экстремумам ряда.

    Пивот подтверждается, когда цена разворачивается от последнего экстремума на >= threshold_pct.
    Возвращает список словарей в хронологическом порядке:
        {"index": int, "price": float, "kind": "H"|"L", "confirmed": bool}
    Первый элемент — якорь (index 0); последний — ТЕКУЩИЙ экстремум, ещё не подтверждённый
    разворотом (confirmed=False, честная пометка незавершённой волны). Чередование H/L гарантируется.
    """
    p = _arr(prices)
    n = p.size
    if n < 2:
        raise ValueError("нужно ≥2 точек")
    if not (0.0 < threshold_pct < 1.0):
        raise ValueError("threshold_pct в (0,1)")

    confirmed = []           # подтверждённые внутренние пивоты
    trend = 0                # 0 неизв., +1 вверх, -1 вниз
    ext_idx, ext_price = 0, p[0]

    for i in range(1, n):
        if trend == 0:
            up = (p[i] - p[0]) / p[0]
            dn = (p[0] - p[i]) / p[0]
            if up >= threshold_pct:
                trend, ext_idx, ext_price = 1, i, p[i]
            elif dn >= threshold_pct:
                trend, ext_idx, ext_price = -1, i, p[i]
            else:
                # запоминаем наибольшее отклонение от якоря, чтобы не терять ранний экстремум
                if abs(p[i] - p[0]) > abs(ext_price - p[0]):
                    ext_idx, ext_price = i, p[i]
        elif trend == 1:
            if p[i] >= ext_price:
                ext_idx, ext_price = i, p[i]
            elif (ext_price - p[i]) / ext_price >= threshold_pct:
                confirmed.append({"index": ext_idx, "price": float(ext_price), "kind": "H"})
                trend, ext_idx, ext_price = -1, i, p[i]
        else:  # trend == -1
            if p[i] <= ext_price:
                ext_idx, ext_price = i, p[i]
            elif (p[i] - ext_price) / ext_price >= threshold_pct:
                confirmed.append({"index": ext_idx, "price": float(ext_price), "kind": "L"})
                trend, ext_idx, ext_price = 1, i, p[i]

    # Якорь (index 0): тип противоположен первому подтверждённому пивоту (или текущему тренду).
    if confirmed:
        anchor_kind = "L" if confirmed[0]["kind"] == "H" else "H"
    elif trend != 0:
        anchor_kind = "L" if trend == 1 else "H"
    else:
        anchor_kind = None
    pivots = [{"index": 0, "price": float(p[0]), "kind": anchor_kind, "confirmed": True}]
    for c in confirmed:
        pivots.append({**c, "confirmed": True})

    # Текущий незавершённый экстремум как последний (tentative) пивот.
    last_kind = "H" if trend == 1 else ("L" if trend == -1 else None)
    if ext_idx != pivots[-1]["index"]:
        # тип чередуется относительно предыдущего пивота
        prev_kind = pivots[-1]["kind"]
        if last_kind is None and prev_kind is not None:
            last_kind = "L" if prev_kind == "H" else "H"
        pivots.append({"index": ext_idx, "price": float(ext_price),
                       "kind": last_kind, "confirmed": False})

    # Подчистка: убрать возможные подряд одинаковые типы (берём более экстремальный),
    # гарантировать строгое чередование H/L.
    return _enforce_alternation(pivots)


def _enforce_alternation(pivots):
    if len(pivots) <= 1:
        return pivots
    out = [pivots[0]]
    for piv in pivots[1:]:
        prev = out[-1]
        if piv["kind"] == prev["kind"] and piv["kind"] is not None:
            # одинаковый тип подряд — оставляем более экстремальный, сохраняя confirmed-флаг разумно
            keep_new = (piv["kind"] == "H" and piv["price"] >= prev["price"]) or \
                       (piv["kind"] == "L" and piv["price"] <= prev["price"])
            if keep_new:
                out[-1] = piv
            # иначе пропускаем новый
        else:
            out.append(piv)
    return out


def nearest_fib(ratio):
    """Ближайший стандартный уровень Фибоначчи и расстояние до него (для интерпретации агентом)."""
    if ratio is None or not np.isfinite(ratio):
        return {"level": None, "distance": None}
    arr = np.array(FIB_LEVELS)
    j = int(np.argmin(np.abs(arr - ratio)))
    return {"level": float(arr[j]), "distance": round(float(abs(arr[j] - ratio)), 4)}


def fib_retracement(start, end, point):
    """Доля отката `point` относительно хода start→end. 0=в end, 1=обратно в start."""
    rng = end - start
    if rng == 0:
        return None
    return round(float((end - point) / rng), 4)


def _leg_len(a, b):
    return abs(float(b) - float(a))


def label_impulse(pivots):
    """Проверяет 6 пивотов (P0..P5) как импульс Эллиотта (5 волн) и меряет отношения.

    Принимает список пивотов (как из zigzag_pivots) ДЛИНОЙ РОВНО 6, строго чередующихся.
    Возвращает dict: направление, валидность по трём жёстким правилам, список нарушений,
    волны с длинами и фибо-отношениями. Это ИЗМЕРЕНИЕ, не вердикт о «единственно верном» счёте.
    """
    if len(pivots) != 6:
        raise ValueError("импульсу нужно ровно 6 пивотов (P0..P5)")
    P = [float(p["price"]) for p in pivots]
    kinds = [p["kind"] for p in pivots]

    # направление: восходящий импульс стартует с L (P0=L,P1=H,...), нисходящий — с H
    if kinds[0] == "L" and kinds[-1] == "H":
        direction = "up"
    elif kinds[0] == "H" and kinds[-1] == "L":
        direction = "down"
    else:
        direction = "up" if P[5] > P[0] else "down"

    L1, L2 = _leg_len(P[0], P[1]), _leg_len(P[1], P[2])
    L3, L4 = _leg_len(P[2], P[3]), _leg_len(P[3], P[4])
    L5 = _leg_len(P[4], P[5])

    violations = []
    # R1: волна 2 не дальше начала волны 1
    if direction == "up":
        if P[2] <= P[0]:
            violations.append("R1: волна 2 откатила за начало волны 1")
        if P[4] <= P[1]:
            r3 = "R3: волна 4 зашла в территорию волны 1 (перекрытие)"
        else:
            r3 = None
    else:
        if P[2] >= P[0]:
            violations.append("R1: волна 2 откатила за начало волны 1")
        if P[4] >= P[1]:
            r3 = "R3: волна 4 зашла в территорию волны 1 (перекрытие)"
        else:
            r3 = None
    # R2: волна 3 не самая короткая из 1,3,5
    if L3 < L1 and L3 < L5:
        violations.append("R2: волна 3 — самая короткая из волн 1/3/5")
    if r3:
        violations.append(r3)

    waves = [
        {"wave": 1, "from": pivots[0]["index"], "to": pivots[1]["index"], "len": round(L1, 6)},
        {"wave": 2, "from": pivots[1]["index"], "to": pivots[2]["index"], "len": round(L2, 6),
         "retrace_of_w1": round(L2 / L1, 4) if L1 else None,
         "fib": nearest_fib(L2 / L1 if L1 else None)},
        {"wave": 3, "from": pivots[2]["index"], "to": pivots[3]["index"], "len": round(L3, 6),
         "ext_of_w1": round(L3 / L1, 4) if L1 else None,
         "fib": nearest_fib(L3 / L1 if L1 else None)},
        {"wave": 4, "from": pivots[3]["index"], "to": pivots[4]["index"], "len": round(L4, 6),
         "retrace_of_w3": round(L4 / L3, 4) if L3 else None,
         "fib": nearest_fib(L4 / L3 if L3 else None)},
        {"wave": 5, "from": pivots[4]["index"], "to": pivots[5]["index"], "len": round(L5, 6),
         "ratio_to_w1": round(L5 / L1, 4) if L1 else None,
         "fib": nearest_fib(L5 / L1 if L1 else None)},
    ]
    return {
        "direction": direction,
        "valid": not violations,
        "violations": violations,
        "waves": waves,
        "pivots_idx": [p["index"] for p in pivots],
    }


def label_correction(pivots):
    """Измеряет ABC-коррекцию (4 пивота: старт, A, B, C). Коррекции гибки — жёстких правил нет,
    возвращаем измеримые отношения (ретрейс B от A, отношение C к A) для интерпретации агентом."""
    if len(pivots) != 4:
        raise ValueError("коррекции нужно ровно 4 пивота (старт, A, B, C)")
    P = [float(p["price"]) for p in pivots]
    LA, LB, LC = _leg_len(P[0], P[1]), _leg_len(P[1], P[2]), _leg_len(P[2], P[3])
    return {
        "A": {"from": pivots[0]["index"], "to": pivots[1]["index"], "len": round(LA, 6)},
        "B": {"from": pivots[1]["index"], "to": pivots[2]["index"], "len": round(LB, 6),
              "retrace_of_A": round(LB / LA, 4) if LA else None,
              "fib": nearest_fib(LB / LA if LA else None)},
        "C": {"from": pivots[2]["index"], "to": pivots[3]["index"], "len": round(LC, 6),
              "ratio_to_A": round(LC / LA if LA else None, 4) if LA else None,
              "fib": nearest_fib(LC / LA if LA else None)},
    }


def wave_markup(prices, threshold_pct=0.05, recent_pivots=10):
    """Полная числовая разметка для агента-волновика. НЕ выносит «истинный счёт» (П8):
    даёт пивоты, попытку разметки последнего импульса (6 пивотов) с проверкой 3 правил и
    последней ABC-коррекции (4 пивота). Недостаток данных — честно помечается.

    Возвращает dict, пригодный для подачи в срез контекста волновика.
    """
    p = _arr(prices)
    out = {
        "method": "ZigZag по фактическим экстремумам + 3 жёстких правила Эллиотта; счёт неоднозначен — альтернатива за агентом",
        "threshold_pct": threshold_pct,
        "n_bars": int(p.size),
    }
    if p.size < 10:
        out["note"] = "нет данных: < 10 баров — разметка волн не строится"
        out["pivots"] = []
        return out

    pivots = zigzag_pivots(p, threshold_pct)
    out["n_pivots"] = len(pivots)
    out["pivots"] = pivots[-recent_pivots:]

    if len(pivots) >= 6:
        try:
            out["impulse_last"] = label_impulse(pivots[-6:])
        except ValueError as e:
            out["impulse_last"] = {"note": f"импульс не размечен: {e}"}
    else:
        out["impulse_last"] = {"note": f"нет данных: пивотов {len(pivots)} < 6 для импульса"}

    if len(pivots) >= 4:
        try:
            out["correction_last"] = label_correction(pivots[-4:])
        except ValueError as e:
            out["correction_last"] = {"note": f"коррекция не размечена: {e}"}
    else:
        out["correction_last"] = {"note": f"нет данных: пивотов {len(pivots)} < 4 для ABC"}

    out["ambiguity_note"] = ("волновой счёт по природе неоднозначен; код дал измеримые пивоты и "
                             "проверку жёстких правил — альтернативный счёт и интерпретацию даёт агент")
    return out
