# -*- coding: utf-8 -*-
"""mathlib/behavioral.py — ДЕТЕРМИНИРОВАННЫЕ поведенческие прокси (REVISION §R4, Этап R4a).

Роль A (операционализированный сигнал): измеримые следы крауд-психологии → ЧИСЛО (инвариант 6 —
считает КОД, не LLM). Кормят срез behavioral/non_obviousness/timeliness/risk. Роль B (нарратив,
объяснение) — у агентов, не здесь.

ВАЖНО: прокси ЗАТУХАЮТ и зависят от режима (известный сигнал арбитражируется) → пере-валидируются
петлёй (§R6); здесь НЕ зашиты как вечная истина. Нет данных → None с пометкой (П8), не выдумка.

borrow-давление: EODHD НЕ продаёт cost-to-borrow → строим ПРОКСИ из short%float + Δ short interest
(наращивают/крывают) + days-to-cover (ShortRatio) + put-skew опционов. Информативнее голой ставки:
говорит и «дорого/опасно шортить», и «насколько перегрет и хрупок шорт» (риск сквиза).
"""
import math


def _clip01(x):
    return max(0.0, min(1.0, float(x)))


def borrow_pressure(*, short_pct_float=None, shares_short=None, shares_short_prior=None,
                    short_ratio=None, put_skew=None):
    """Прокси borrow-давления / риска сквиза ∈ [0,1] (§R4). Усредняет ДОСТУПНЫЕ компоненты; None-вход
    не учитывается (П8 — не выдумываем). Компоненты:
      • уровень short%float (выше → дороже/опаснее шортить; 20%+ ≈ максимум);
      • Δ short interest (наращивают d>0 → выше; крывают → ниже);
      • days-to-cover (ShortRatio: 8+ дней ≈ высокий потенциал сквиза);
      • put-skew (рынок дорого закладывает hard-to-borrow / downside).
    Возвращает {score, компоненты, провенанс, n_inputs}. score=None, если ни одного входа."""
    comps, prov = {}, []
    if short_pct_float is not None:
        comps["уровень_шорта"] = _clip01(float(short_pct_float) / 0.20)
        prov.append(f"short%float={round(float(short_pct_float), 4)}")
    if shares_short is not None and shares_short_prior not in (None, 0):
        d = (float(shares_short) - float(shares_short_prior)) / float(shares_short_prior)
        comps["Δ_шорта"] = _clip01(0.5 + d)            # наращивают → >0.5, крывают → <0.5
        prov.append(f"Δshort={round(d, 3)}")
    if short_ratio is not None:
        comps["дни_покрытия"] = _clip01(float(short_ratio) / 8.0)
        prov.append(f"days-to-cover={round(float(short_ratio), 2)}")
    if put_skew is not None:
        comps["put_skew"] = _clip01(float(put_skew))
        prov.append(f"put_skew={round(float(put_skew), 3)}")
    if not comps:
        return {"score": None, "компоненты": {}, "провенанс": "нет данных (П8)", "n_inputs": 0}
    return {"score": round(sum(comps.values()) / len(comps), 4), "компоненты": comps,
            "провенанс": "; ".join(prov), "n_inputs": len(comps)}


def overextension(prices, *, window=50):
    """Перегрев/растяжение от средней (z-подобный, как полоса Боллинджера): (last − MA)/σ уровня за
    окно. >0 = выше средней (перекуплено, «когда хватит покупать популярное»), <0 = перепродано.
    None — мало истории или нулевая дисперсия (П8)."""
    import numpy as np
    p = np.asarray([x for x in (prices or []) if x is not None], dtype=float)
    if p.size < window + 1:
        return None
    w = p[-window:]
    ma, sd = float(np.mean(w)), float(np.std(w))
    if not (sd > 0):
        return None
    return round((float(p[-1]) - ma) / sd, 4)


def attention(*, now, baseline):
    """Всплеск внимания (ограниченное внимание / салиентность): log(now/baseline). >0 = всплеск (тема
    разогрета → вероятнее уже в цене/очевидна — питает НЕОЧЕВИДНОСТЬ). None — нет данных (П8)."""
    if now is None or baseline in (None, 0):
        return None
    if float(now) <= 0 or float(baseline) <= 0:
        return None
    return round(math.log(float(now) / float(baseline)), 4)


def behavioral_context(*, prices=None, short=None, options=None, attention_pair=None):
    """Свести доступные прокси в один срез для агентов. Каждый блок независим: нет данных → None (П8).
      short: {short_pct_float, shares_short, shares_short_prior, short_ratio};
      options: {put_skew}; attention_pair: {now, baseline}."""
    short = short or {}
    options = options or {}
    bp = borrow_pressure(short_pct_float=short.get("short_pct_float"),
                         shares_short=short.get("shares_short"),
                         shares_short_prior=short.get("shares_short_prior"),
                         short_ratio=short.get("short_ratio"),
                         put_skew=options.get("put_skew"))
    over = overextension(prices) if prices else None
    att = attention(**attention_pair) if attention_pair else None
    return {"borrow_давление": bp, "перегрев": over, "всплеск_внимания": att,
            "примечание": "прокси крауд-психологии (роль A, число); нарратив — у агента. "
                          "затухают → пере-валидация петлёй §R6 (П8/§R4)"}
