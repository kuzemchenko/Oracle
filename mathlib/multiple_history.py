# -*- coding: utf-8 -*-
"""mathlib/multiple_history.py — оценка компании против её собственной истории (Q-в-цене, остаток, 09.11).

Вопрос продукта: «акция дешёвая?» — бессмысленно без сравнения. Здесь — P/E (капитализация к прибыли за
4 квартала, по официальной отчётности GAAP) и P/S (капитализация к выручке за 4 квартала) СЕЙЧАС против тех
же мультипликаторов на конец каждого квартала за последние N лет: перцентиль, медиана, минимум, максимум.
Низкий мультипликатор сам по себе НЕ довод (часто рынок прав, что бизнес хуже) — это только мерило того,
насколько ожидания сейчас выше или ниже обычного для ЭТОЙ компании.

Квартал засчитывается с даты подачи отчёта (filing_date): на конец квартала цифр ещё нет — так нет
заглядывания вперёд. Убыточные периоды для P/E не считаются (П8: «не считается», а не ноль).
Детерминированно (Инв#6), входы — в результате.
"""
import bisect
import datetime


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def quarterly(rows, field):
    """EODHD Financials::…::quarterly (словарь по датам) → [(дата_квартала, дата_подачи, значение)] по возрастанию."""
    out = []
    for k, r in sorted((rows or {}).items()):
        v = _f((r or {}).get(field))
        if v is None:
            continue
        out.append((str(r.get("date") or k)[:10], str(r.get("filing_date") or r.get("date") or k)[:10], v))
    return out


def ttm(series):
    """Сумма 4 последовательных кварталов → [(дата_квартала, дата_подачи, сумма)]. Разрыв > 120 дней — не склеиваем."""
    out = []
    for i in range(3, len(series)):
        win = series[i - 3:i + 1]
        ds = [datetime.date.fromisoformat(w[0]) for w in win]
        if all(60 <= (ds[j + 1] - ds[j]).days <= 120 for j in range(3)):
            out.append((win[-1][0], win[-1][1], sum(w[2] for w in win)))
    return out


def _asof(series, day):
    """Последнее значение, ПОДАННОЕ не позже day (по дате подачи)."""
    best = None
    for q, filed, v in series:
        if filed <= day:
            best = (q, filed, v)
    return best


def _close_at(dates, closes, day):
    i = bisect.bisect_right(dates, day) - 1
    return (dates[i], closes[i]) if i >= 0 else (None, None)


def history(dates, closes, flow_ttm, shares, years=5, today=None):
    """Мультипликатор на конец каждого квартала за years лет и сейчас. flow_ttm — ttm() прибыли или выручки;
    shares — quarterly() числа акций. → dict или {"ok": False, "причина"}."""
    if not dates or not flow_ttm or not shares:
        return {"ok": False, "причина": "нет цен, отчётности или числа акций"}
    today = str(today or dates[-1])[:10]
    start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=int(365.25 * years))).isoformat()
    points = []
    for q, filed, _v in flow_ttm:
        if not (start <= filed <= today):
            continue
        f = _asof(flow_ttm, filed)
        s = _asof(shares, filed)
        d, c = _close_at(dates, closes, filed)
        if not f or not s or c is None or f[2] <= 0:
            continue
        points.append({"дата": filed, "квартал": f[0], "мультипликатор": round(c * s[2] / f[2], 2)})
    f = _asof(flow_ttm, today)
    s = _asof(shares, today)
    d, c = _close_at(dates, closes, today)
    if not f or not s or c is None:
        return {"ok": False, "причина": "нет свежей отчётности или цены"}
    if f[2] <= 0:
        return {"ok": False, "причина": "за последние 4 квартала убыток/ноль — мультипликатор не считается"}
    now = round(c * s[2] / f[2], 2)
    vals = sorted(p["мультипликатор"] for p in points)
    if len(vals) < 8:
        return {"ok": False, "причина": f"мало истории: {len(vals)} кварталов с положительным показателем за {years} лет",
                "сейчас": now}
    below = sum(1 for v in vals if v < now)
    med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    return {"ok": True, "сейчас": now, "цена": [d, c], "отчёт": {"квартал": f[0], "подан": f[1], "ttm": f[2]},
            "акций": s[2], "кварталов": len(vals), "перцентиль": round(below / len(vals), 3),
            "медиана": round(med, 2), "мин": vals[0], "макс": vals[-1], "лет": years, "точки": points}


def words(name, res):
    if not res.get("ok"):
        return f"{name}: не посчитан — {res.get('причина')}."
    pct = round(res["перцентиль"] * 100)
    side = f"выше, чем в {pct}%" if pct >= 50 else f"ниже, чем в {100 - pct}%"
    return (f"{name} сейчас {res['сейчас']:.1f} — {side} кварталов за {res['лет']} лет "
            f"(обычно {res['медиана']:.1f}, разброс {res['мин']:.1f}–{res['макс']:.1f}). "
            "Это мерило ожиданий, не довод «дёшево/дорого».")
