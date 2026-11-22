# -*- coding: utf-8 -*-
"""mathlib/independence.py — НЕЗАВИСИМОСТЬ СТАВОК (Q-независимость-ставок). Инв#6: только расчёт.

Владелец 22.11 (разбор модели Рэя Далио): «святой Грааль» — 15–20 ставок, не зависящих друг от друга, снижают риск
без потери доходности. Находка того же дня: все 8 ноябрьских идей системы были покупками и упали вместе с рынком —
по сути одна ставка «рынок вырастет», повторённая восемь раз. Комитет оценивал каждую идею отдельно.

Что считает код:
  • связь двух бумаг — корреляция их дневных доходностей за последние N торговых дней (по общим датам);
  • связь двух СТАВОК — та же корреляция со знаком сторон: ставка на падение бумаги, которая ходит вместе с нашей
    покупкой, — не повтор, а противовес (связь с минусом);
  • число независимых ставок — n / (1 + (n − 1) × средняя связь): восемь ставок со связью 0.8 — это 1.2 ставки;
  • вывод о новой идее: «новая» / «частичный повтор» / «повтор» уже открытой ставки.
Мало общих дней → связь не измерена (None): это не «независима», а «не знаем».
"""
import math

DEFAULTS = {"дней": 120, "мин_точек": 60, "порог_повтора": 0.7, "порог_связи": 0.4}
NEW, PARTIAL, REPEAT, SAME, UNKNOWN = "новая", "частичный повтор", "повтор", "уже открыта", "не измерено"


def _cfg(rules):
    return {**DEFAULTS, **{k: v for k, v in (rules or {}).items() if k in DEFAULTS}}


def returns(closes):
    """[(дата, цена)] → {дата: дневная доходность}."""
    out, prev = {}, None
    for d, c in closes or []:
        if c is None:
            continue
        c = float(c)
        if prev and prev > 0 and c > 0:
            out[str(d)] = c / prev - 1
        prev = c
    return out


def correlation(a_closes, b_closes, days=120, min_points=60):
    """Корреляция дневных доходностей по общим датам за последние days торговых дней. Мало точек или ряд без
    движения → None."""
    ra, rb = returns(a_closes), returns(b_closes)
    common = sorted(set(ra) & set(rb))[-int(days):]
    if len(common) < int(min_points):
        return None
    x, y = [ra[d] for d in common], [rb[d] for d in common]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx == 0 or sy == 0:
        return None
    return round(sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy), 4)


def bet_link(corr, side_a, side_b):
    """Связь двух ставок: корреляция бумаг со знаком сторон (разные стороны — противовес)."""
    if corr is None:
        return None
    return corr if side_a == side_b else -corr


def effective_n(bets, corr_fn):
    """Сколько НЕЗАВИСИМЫХ ставок в наборе: n / (1 + (n − 1) × средняя связь). Неизмеренные пары в среднюю не входят;
    отрицательная средняя связь считается нулём (противовесы не делают ставок больше, чем их есть).
    → (число | None, средняя связь | None, измеренных пар)."""
    n = len(bets)
    if n == 0:
        return None, None, 0
    if n == 1:
        return 1.0, None, 0
    links = []
    for i in range(n):
        for j in range(i + 1, n):
            c = bet_link(corr_fn(bets[i]["тикер"], bets[j]["тикер"]), bets[i]["сторона"], bets[j]["сторона"])
            if c is not None:
                links.append(c)
    if not links:
        return None, None, 0
    avg = sum(links) / len(links)
    return round(n / (1 + (n - 1) * max(0.0, avg)), 1), round(avg, 2), len(links)


def assess(new, opened, corr_fn, rules=None):
    """new — {'тикер','сторона'}; opened — открытые ставки [{'тикер','сторона','откуда'}]; corr_fn(t1, t2) → корреляция | None.
    → {'вывод', 'похожие', 'открыто', 'та_же_сторона', 'независимых_сейчас', 'независимых_с_новой', 'средняя_связь'}"""
    r = _cfg(rules)
    same_bet = [b for b in opened if b["тикер"] == new["тикер"] and b["сторона"] == new["сторона"]]
    others = [b for b in opened if b["тикер"] != new["тикер"]]
    links = []
    for b in others:
        c = bet_link(corr_fn(new["тикер"], b["тикер"]), new["сторона"], b["сторона"])
        links.append({"тикер": b["тикер"], "сторона": b["сторона"], "откуда": b.get("откуда"), "связь": c})
    measured = sorted([x for x in links if x["связь"] is not None], key=lambda x: -x["связь"])
    top = measured[0] if measured else None
    if same_bet:
        verdict = SAME
    elif not others:
        verdict = NEW
    elif top is None:
        verdict = UNKNOWN
    elif top["связь"] >= r["порог_повтора"]:
        verdict = REPEAT
    elif top["связь"] >= r["порог_связи"]:
        verdict = PARTIAL
    else:
        verdict = NEW
    n_now, avg_now, _ = effective_n(opened, corr_fn)
    with_new = opened if same_bet else opened + [new]
    n_new, _avg, _ = effective_n(with_new, corr_fn)
    return {"вывод": verdict, "похожие": measured[:3], "открыто": len(opened),
            "та_же_сторона": sum(1 for b in opened if b["сторона"] == new["сторона"]),
            "независимых_сейчас": n_now, "независимых_с_новой": n_new, "ставок_с_новой": len(with_new),
            "средняя_связь": avg_now, "пороги": {"повтор": r["порог_повтора"], "связь": r["порог_связи"], "дней": r["дней"]}}
