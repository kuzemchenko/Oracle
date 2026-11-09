# -*- coding: utf-8 -*-
"""mathlib/options_implied.py — «что рынок уже ждёт» по ценам опционов (очередь Q-в-цене, 09.11).

Вопрос продукта: идея говорит «акция будет выше X к дате D». Опционы на эту акцию — деньги, которые рынок
уже поставил на её будущий разброс. По ним можно посчитать:
  • ожидаемый ход до даты (±1σ): spot × IV × √(дни/365) — «рынок закладывает колебание ±N%»;
  • риск-нейтральную вероятность закрыться выше/ниже порога к экспирации: N(±d2) по IV ближайшего страйка.
Это НЕ реальная вероятность (в ней премия за риск и спрос на страховку), а мерило «сколько рынок уже
закладывает». Наш тезис должен объяснить, почему мы видим больше/иначе.

Детерминированно (Инв#6), каждый результат несёт входы (П8). Мало ликвидности или нет нужной даты —
честный отказ или явная оговорка, а не число.
"""
import datetime
import math

MIN_OI = 10            # открытый интерес по выбранной экспирации ниже — опционы «тонкие», оговорка
MAX_REL_SPREAD = 0.5   # (ask−bid)/mid выше — котировка ненадёжна, контракт не берём
DAYS_YEAR = 365.0


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _d(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def parse_chain(rows):
    """Строки EODHD mp/unicornbay/options/eod (data[*].attributes или плоские) → список контрактов.
    Контракты без IV, без страйка или с ненадёжной котировкой отбрасываются (с причиной в счётчике)."""
    out, dropped = [], {"нет_iv": 0, "нет_страйка": 0, "широкий_спред": 0}
    for r in rows or []:
        a = r.get("attributes") if isinstance(r, dict) and isinstance(r.get("attributes"), dict) else r
        if not isinstance(a, dict):
            continue
        k, iv = _f(a.get("strike")), _f(a.get("volatility"))
        if k is None or k <= 0:
            dropped["нет_страйка"] += 1
            continue
        if iv is None or iv <= 0 or iv > 5:
            dropped["нет_iv"] += 1
            continue
        bid, ask = _f(a.get("bid")), _f(a.get("ask"))
        if bid is not None and ask is not None and ask > 0:
            mid = (bid + ask) / 2
            if mid <= 0 or (ask - bid) / mid > MAX_REL_SPREAD:
                dropped["широкий_спред"] += 1
                continue
        out.append({"exp": str(a.get("exp_date"))[:10], "type": str(a.get("type") or "").lower(), "strike": k,
                    "iv": iv, "oi": _f(a.get("open_interest")) or 0.0, "bid": bid, "ask": ask,
                    "on": str(a.get("previous_date") or a.get("bid_date") or "")[:10] or None})
    return out, dropped


def pick_expiry(chain, target_date):
    """Ближайшая экспирация НЕ раньше даты идеи; если таких нет — последняя доступная (с оговоркой)."""
    t = _d(target_date)
    exps = sorted({c["exp"] for c in chain if _d(c["exp"])})
    if not exps or t is None:
        return None, "нет экспираций или даты"
    after = [e for e in exps if _d(e) >= t]
    if after:
        return after[0], None
    return exps[-1], f"опционов до {target_date} нет — взята последняя экспирация {exps[-1]}"


def iv_at(chain, exp, strike):
    """IV на страйке: линейная интерполяция между соседними страйками (среднее call/put на страйке)."""
    by_k = {}
    for c in chain:
        if c["exp"] == exp:
            by_k.setdefault(c["strike"], []).append(c["iv"])
    if not by_k:
        return None
    ks = sorted(by_k)
    ivs = [sum(by_k[k]) / len(by_k[k]) for k in ks]
    if strike <= ks[0]:
        return ivs[0]
    if strike >= ks[-1]:
        return ivs[-1]
    for i in range(1, len(ks)):
        if ks[i] >= strike:
            w = (strike - ks[i - 1]) / (ks[i] - ks[i - 1])
            return ivs[i - 1] + w * (ivs[i] - ivs[i - 1])
    return ivs[-1]


def implied_move(spot, iv, days):
    """Ожидаемый ход ±1σ до даты: доля и в деньгах."""
    if not spot or spot <= 0 or not iv or days is None or days <= 0:
        return None
    m = iv * math.sqrt(days / DAYS_YEAR)
    return {"доля": round(m, 4), "в_деньгах": round(spot * m, 2), "диапазон_68": [round(spot * (1 - m), 2),
                                                                                  round(spot * (1 + m), 2)]}


def prob_beyond(spot, threshold, iv, days, direction="above", r=0.0):
    """Риск-нейтральная вероятность закрыться выше (above) / ниже (below) порога к сроку: N(±d2)."""
    if not spot or not threshold or spot <= 0 or threshold <= 0 or not iv or not days or days <= 0:
        return None
    t = days / DAYS_YEAR
    d2 = (math.log(spot / threshold) + (r - 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    p = _ncdf(d2) if direction == "above" else _ncdf(-d2)
    return round(p, 4)


def market_expectation(rows, spot, today, resolve_by, threshold=None, direction="above", spot_date=None):
    """Главная функция: что опционы закладывают к дате идеи. → dict (ok, входы, экспирация, IV, ход, вероятность)."""
    chain, dropped = parse_chain(rows)
    base = {"входы": {"цена": spot, "дата_цены": spot_date, "сегодня": str(today)[:10], "срок_идеи": resolve_by,
                      "порог": threshold, "направление": direction},
            "отброшено_контрактов": dropped, "контрактов": len(chain),
            "метод": "IV опционов EODHD (Unicorn Bay) → ход ±1σ = цена×IV×√(дни/365); вероятность — N(d2), "
                     "риск-нейтральная (не реальная): мерило того, что рынок уже закладывает"}
    if not chain:
        return {**base, "ok": False, "причина": "нет пригодных опционов (нет IV или котировки ненадёжны)"}
    if not spot or spot <= 0:
        return {**base, "ok": False, "причина": "нет текущей цены акции"}
    exp, note = pick_expiry(chain, resolve_by)
    if not exp:
        return {**base, "ok": False, "причина": note}
    days = (_d(exp) - _d(today)).days if _d(today) else None
    if not days or days <= 0:
        return {**base, "ok": False, "причина": f"экспирация {exp} не позже сегодняшней даты"}
    atm = iv_at(chain, exp, spot)
    oi = sum(c["oi"] for c in chain if c["exp"] == exp)
    out = {**base, "ok": True, "экспирация": exp, "дней": days, "iv_на_деньгах": round(atm, 4),
           "ход_1сигма": implied_move(spot, atm, days), "открытый_интерес": oi, "оговорки": []}
    if note:
        out["оговорки"].append(note)
    if oi < MIN_OI:
        out["оговорки"].append(f"опционы тонкие: открытый интерес {oi:.0f} < {MIN_OI} — оценка грубая")
    on = sorted({c["on"] for c in chain if c["exp"] == exp and c["on"]})
    if on:
        out["котировки_на"] = on[-1]
    if threshold:
        iv_k = iv_at(chain, exp, float(threshold))
        out["iv_на_пороге"] = round(iv_k, 4)
        out["вероятность_рн"] = prob_beyond(spot, float(threshold), iv_k, days, direction)
        out["нужный_ход"] = round(float(threshold) / spot - 1.0, 4)
        mv = out["ход_1сигма"]["доля"]
        out["нужный_ход_в_сигмах"] = round(out["нужный_ход"] / mv, 2) if mv else None
    return out


def words(res):
    """Одна-две фразы для владельца (без жаргона). Отказ — честно."""
    if not res.get("ok"):
        return f"Опционы: оценка не посчитана — {res.get('причина')}."
    mv = res["ход_1сигма"]
    s = (f"Опционы до {res['экспирация']} закладывают колебание примерно ±{mv['доля'] * 100:.0f}% "
         f"(от ${mv['диапазон_68'][0]:.2f} до ${mv['диапазон_68'][1]:.2f} в двух случаях из трёх)")
    if res.get("вероятность_рн") is not None:
        side = "выше" if res["входы"]["направление"] == "above" else "ниже"
        s += (f"; закрыться {side} ${float(res['входы']['порог']):.2f} рынок оценивает примерно в "
              f"{res['вероятность_рн'] * 100:.0f}% (по ценам опционов, с премией за риск — не точная вероятность)")
    s += "."
    if res.get("оговорки"):
        s += " Оговорка: " + "; ".join(res["оговорки"]) + "."
    return s
