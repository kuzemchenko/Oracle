# -*- coding: utf-8 -*-
"""mathlib/market_moves.py — «резкое движение» цены против собственной истории бумаги (Q-меню-рынок). Инв#6.

Зачем: меню событий 22.11 предложило «атаку на судно в Балтике», а падение широкого рынка США на 9% за неделю
не увидело — оно смотрело только новости. Здесь чистый расчёт по ряду цен закрытия, без моделей и без сети:

  • ход за 1 и за 5 торговых дней (последняя цена против цены 1 и 5 торговых дней назад);
  • «обычное колебание» бумаги — типичный размер такого же хода за последний год (252 торговых дня ДО
    измеряемого окна, чтобы сегодняшний обвал не раздувал собственную норму): корень из среднего квадрата
    дневных (или пятидневных, внахлёст) ходов;
  • кратность = ход / обычное колебание: «упала за день в 4 раза сильнее, чем обычно колеблется за день».

«Резкое движение» (порог понятный и один на всех): ход за день ИЛИ за 5 дней не меньше 3 обычных колебаний
и не меньше 2% по величине (второе условие отсекает «тихие» бумаги вроде фонда краткосрочных облигаций, где
3 обычных колебания — это десятые доли процента).

Разрыв ряда (дневной ход больше «разрыв», по умолчанию 45%) — почти всегда дробление акций без поправки
прежних цен в базе, а не рынок: в истории такой день в норму не идёт, а если он попал в измеряемое окно —
ход не измерен (None), а не ложное «−70% за день».

Дыра в датах (ревью 22.11): ход считается по соседним строкам ряда, поэтому если у ряда есть даты и между концами
измеряемого окна прошло больше календарных дней, чем бывает у стольких торговых дней (выходные и праздники
учтены: до 6 дней на «день», до 11 на «5 дней»), — в базе пропущены торги, «ход за день» был бы ходом за недели →
не измерен (None). Ряд без дат этой проверки не проходит (нечем проверять).

Мало истории → None (честное «нет данных», П8). Это измерение «что случилось», а не прогноз.
"""
import datetime
import math

DEFAULTS = {"дней_истории": 252, "мин_наблюдений": 120, "порог_кратности": 3.0, "мин_ход": 0.02, "разрыв": 0.45}
WINDOWS = (("день", 1), ("неделя", 5))


def _params(rules=None):
    return dict(DEFAULTS, **{k: v for k, v in (rules or {}).items() if k in DEFAULTS and v is not None})


def _series(closes):
    """[(дата, цена)] или [цена] по возрастанию даты → ([даты | None], [цены]); пустые и неположительные цены —
    пропуск (в базе встречаются нули у новых бумаг)."""
    dates, vals = [], []
    for row in closes or []:
        d, c = (row[0], row[1]) if isinstance(row, (tuple, list)) else (None, row)
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(c) or c <= 0:
            continue
        dates.append(d)
        vals.append(c)
    return dates, vals


def daily_returns(vals, gap=None):
    """Дневные ходы (доля) по ряду цен; ход по величине не меньше gap — разрыв ряда → None на этом месте."""
    out = []
    for i in range(1, len(vals)):
        r = vals[i] / vals[i - 1] - 1
        out.append(None if (gap is not None and abs(r) >= gap) else r)
    return out


def window_returns(rets, days):
    """Ходы за `days` дней внахлёст: для каждого места — произведение (1+ход) по окну; в окне есть разрыв → None."""
    out = []
    for i in range(days - 1, len(rets)):
        w = rets[i - days + 1:i + 1]
        if any(x is None for x in w):
            out.append(None)
            continue
        p = 1.0
        for x in w:
            p *= 1 + x
        out.append(p - 1)
    return out


def typical_move(rets, days, lookback=None, min_obs=None):
    """Обычное колебание за `days` дней: корень из среднего квадрата ходов за `days` дней по последним `lookback`
    дневным ходам ряда `rets` (сам ряд — уже БЕЗ измеряемого окна). → (величина, число наблюдений) или None."""
    p = _params()
    lookback = int(lookback or p["дней_истории"])
    min_obs = int(min_obs or p["мин_наблюдений"])
    obs = [x for x in window_returns(rets[-lookback:], days) if x is not None]
    if len(obs) < min_obs:
        return None
    rms = math.sqrt(sum(x * x for x in obs) / len(obs))
    return (rms, len(obs)) if rms > 0 else None


def max_calendar_span(days):
    """Сколько календарных дней самое большее занимают `days` торговых: 5 торговых — это 7 календарных, плюс запас
    4 дня на праздники (два праздника в окне на стыке декабря и января)."""
    return math.ceil(days * 7 / 5) + 4


def window_has_hole(dates, days):
    """Между концами окна в `days` последних строк прошло больше max_calendar_span — в ряду пропущены торги.
    Дат нет или они не читаются → False (нечем проверять)."""
    if len(dates) < days + 1:
        return False
    try:
        d0 = datetime.date.fromisoformat(str(dates[-1 - days])[:10])
        d1 = datetime.date.fromisoformat(str(dates[-1])[:10])
    except (TypeError, ValueError):
        return False
    return (d1 - d0).days > max_calendar_span(days)


def measure(closes, days=1, rules=None):
    """Ход за `days` торговых дней и его необычность. → {ход, обычное, кратность, наблюдений, дата} или None
    (мало истории, разрыв ряда или дыра в датах в измеряемом окне, нулевая норма). Кратность со знаком хода."""
    p = _params(rules)
    dates, vals = _series(closes)
    if len(vals) < days + 2 or window_has_hole(dates, days):
        return None
    rets = daily_returns(vals, p["разрыв"])
    cur = rets[-days:]
    if any(x is None for x in cur):
        return None
    typ = typical_move(rets[:-days], days, p["дней_истории"], p["мин_наблюдений"])
    if typ is None:
        return None
    move = vals[-1] / vals[-1 - days] - 1
    return {"ход": round(move, 4), "обычное": round(typ[0], 4), "кратность": round(move / typ[0], 2),
            "наблюдений": typ[1], "дата": None if dates[-1] is None else str(dates[-1])}


def is_sharp(m, rules=None):
    """Мера measure → True, если ход не меньше порога кратности И не меньше минимального хода; None → False."""
    if not m:
        return False
    p = _params(rules)
    return abs(m["кратность"]) >= p["порог_кратности"] and abs(m["ход"]) >= p["мин_ход"]


def window_has_gap(closes, days=5, rules=None):
    """В последних `days` дневных ходах есть разрыв ряда (подозрение на дробление акций без поправки цен)."""
    p = _params(rules)
    _, vals = _series(closes)
    return any(x is None for x in daily_returns(vals, p["разрыв"])[-days:])


def summary(closes, rules=None):
    """Сводка по бумаге: меры за день и за 5 дней и признак «резкое движение».
    → {день, неделя, резкое, окно, сила, вниз, дата} или None, если не измерен ни один срок.
    «окно» — срок, на котором движение сильнее по кратности (среди резких, если они есть); «сила» — его кратность
    по величине; «вниз» — знак хода на этом сроке."""
    ms = {name: measure(closes, days, rules) for name, days in WINDOWS}
    if not any(ms.values()):
        return None
    sharp = [n for n, _ in WINDOWS if is_sharp(ms[n], rules)]
    pool = sharp or [n for n, _ in WINDOWS if ms[n]]
    lead = max(pool, key=lambda n: abs(ms[n]["кратность"]))
    return {"день": ms["день"], "неделя": ms["неделя"], "резкое": bool(sharp), "окно": lead,
            "сила": abs(ms[lead]["кратность"]), "вниз": ms[lead]["ход"] < 0,
            "дата": (ms["день"] or ms["неделя"])["дата"]}
