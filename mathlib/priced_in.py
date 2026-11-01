# -*- coding: utf-8 -*-
"""mathlib/priced_in.py — модуль «что уже в цене» (ПОДПИСАН владельцем 01.11, пакет подач).

Вопрос продукта: если механизм исторически даёт ход +30–130%, а актив уже прошёл +40% —
сколько прецедентного хода осталось? Честный ответ — ВИЛКА (реализовано/верх … реализовано/низ
прецедентного диапазона), не одно число: прецеденты сами разбросаны.

Детерминированно (Инв#6): парсер амплитуд из текста библиотеки механизмов
(формы «+30–130%», «−3…−8%», «×2–3», «±8–31%»), доля отыгранности, вердикт словами
{РАНО / В ПУТИ / БОЛЬШАЯ ЧАСТЬ В ЦЕНЕ / ПЕРЕЛЁТ / ПРОТИВ МЕХАНИЗМА}. Не распозналось —
честный ОТКАЗ (П8), без выдуманных процентов.

Источник реализованного хода (дневные закрытия сейчас; фьючерсные кривые/опционные
ожидания EODHD — серверный слой V6 поверх той же математики) — забота вызывающего.
"""
import re

# «+30–130%», «−3…−8%», «±8–31%», «+10%»  (минус: -, −, …; тире: –, -, —, …)
_PCT = r"[+\-−±]?\s*\d+(?:[.,]\d+)?"
_SEP = r"\s*(?:[–\-—]|…|\.\.\.)\s*"
PCT_RANGE_RE = re.compile(rf"({_PCT}){_SEP}({_PCT})\s*%")
PCT_SINGLE_RE = re.compile(rf"({_PCT})\s*%")
MULT_RANGE_RE = re.compile(rf"[×x]\s*(\d+(?:[.,]\d+)?)(?:{_SEP}(\d+(?:[.,]\d+)?))?")

РАНО, В_ПУТИ, В_ЦЕНЕ, ПЕРЕЛЁТ, ПРОТИВ = (
    "РАНО", "В ПУТИ", "БОЛЬШАЯ ЧАСТЬ В ЦЕНЕ", "ПЕРЕЛЁТ", "ПРОТИВ МЕХАНИЗМА")


def _num(s):
    return float(s.replace("−", "-").replace("±", "").replace(",", ".").replace(" ", ""))


def parse_amplitude(text):
    """Текст амплитуды механизма → список диапазонов [{'низ','верх','вид'}] в ПРОЦЕНТАХ,
    в порядке появления (первый — обычно 1-й ход). «×2–3» → +100…+200%. Пусто → []."""
    s = str(text or "")
    found = []  # (позиция, низ, верх, вид)
    for m in PCT_RANGE_RE.finditer(s):
        a, b = _num(m.group(1)), _num(m.group(2))
        two_sided = m.group(1).lstrip().startswith("±")
        found.append((m.start(), min(a, b), max(a, b),
                      "проценты±" if two_sided else "проценты"))
    taken = [(st, st + 1) for st, *_ in found]
    for m in MULT_RANGE_RE.finditer(s):
        lo = (_num(m.group(1)) - 1.0) * 100.0
        hi = (_num(m.group(2)) - 1.0) * 100.0 if m.group(2) else lo
        found.append((m.start(), min(lo, hi), max(lo, hi), "кратно"))
    covered = set()
    for m in PCT_RANGE_RE.finditer(s):
        covered.update(range(m.start(), m.end()))
    for m in PCT_SINGLE_RE.finditer(s):
        if m.start() not in covered:
            v = _num(m.group(1))
            found.append((m.start(), v, v, "проценты"))
    found.sort(key=lambda t: t[0])
    return [{"низ": round(lo, 2), "верх": round(hi, 2), "вид": kind}
            for _, lo, hi, kind in found]


def played_fraction(realized_pct, amplitude_text, *, range_index=0):
    """Реализованный ход (в %) против прецедентной амплитуды механизма.

    → {'доля_низ','доля_верх','вердикт','строка'} или {'ОТКАЗ': ...} (П8).
    Знак хода против знака амплитуды → ПРОТИВ МЕХАНИЗМА (либо рано, либо правило не работает —
    решает не этот модуль)."""
    ranges = parse_amplitude(amplitude_text)
    if not ranges:
        return {"ОТКАЗ": "амплитуда механизма не распознана из текста — "
                         "долить машинную амплитуду в библиотеку"}
    if range_index >= len(ranges):
        return {"ОТКАЗ": f"в амплитуде {len(ranges)} диапазон(а), запрошен #{range_index}"}
    r = ranges[range_index]
    lo, hi = r["низ"], r["верх"]
    if lo == 0 and hi == 0:
        return {"ОТКАЗ": "прецедентная амплитуда нулевая — доля не определена"}
    try:
        realized = float(realized_pct)
    except (TypeError, ValueError):
        return {"ОТКАЗ": "реализованный ход не число"}
    # знак механизма — по дальнему от нуля краю диапазона (у «−3…−8%» это −8);
    # двусторонняя амплитуда («±8–31%») знак не проверяет
    two_sided = str(r["вид"]).endswith("±")
    far_edge = lo if abs(lo) >= abs(hi) else hi
    if (not two_sided and realized != 0
            and (realized > 0) != (far_edge > 0)):
        return {"доля_низ": 0.0, "доля_верх": 0.0, "вердикт": ПРОТИВ,
                "строка": "цена идёт ПРОТИВ механизма — либо рано, либо правило "
                          "здесь не работает"}
    near, far = sorted((abs(lo), abs(hi)))
    f_lo = abs(realized) / far if far else 0.0
    f_hi = abs(realized) / near if near else f_lo
    mid = (f_lo + f_hi) / 2.0
    if f_lo > 1.2:                  # превышен ДАЛЬНИЙ край прецедентов, не ближний
        verdict = ПЕРЕЛЁТ
    elif mid >= 0.7:
        verdict = В_ЦЕНЕ
    elif mid >= 0.25:
        verdict = В_ПУТИ
    else:
        verdict = РАНО
    lo_pct, hi_pct = int(round(min(f_lo, 1.5) * 100)), int(round(min(f_hi, 1.5) * 100))
    if verdict == ПЕРЕЛЁТ:
        line = ("ход уже ПРЕВЫСИЛ прецедентный диапазон механизма — "
                "покупать остаток не из чего")
    elif lo_pct == hi_pct:
        line = f"пройдено примерно {lo_pct}% прецедентного хода"
    else:
        line = f"пройдено примерно {lo_pct}–{hi_pct}% прецедентного хода"
    return {"доля_низ": round(f_lo, 3), "доля_верх": round(f_hi, 3),
            "вердикт": verdict, "строка": line}
