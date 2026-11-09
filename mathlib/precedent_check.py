# -*- coding: utf-8 -*-
"""mathlib/precedent_check.py — сверка заявленного хода прецедента с настоящими закрытиями (V6, Q-прецеденты 09.11).

В библиотеке механизмов (knowledge/mechanisms.yaml) у каждого прецедента — текст «что было на рынке»,
записанный моделью. Модель хорошо помнит сюжет и плохо — цифры. Здесь код берёт разметку (бумага, даты,
заявленный ход — дословной цитатой из текста) и считает настоящий ход по закрытиям:
  • level — закрытие на конец окна против закрытия до начала;
  • peak  — экстремум внутри окна в сторону заявленного хода против закрытия до начала.
Вердикт: «подтверждён» (знак тот же, величина в пределах ×2), «по знаку» (знак тот же, величина другая),
«не подтверждён» (знак обратный), «нет данных». Детерминированно (Инв#6), все входы — в результате (П8).
"""
import bisect

RATIO_LO, RATIO_HI = 0.5, 2.0


def _at_or_before(dates, closes, day):
    i = bisect.bisect_right(dates, str(day)[:10]) - 1
    return (dates[i], closes[i]) if i >= 0 else (None, None)


def actual_move(dates, closes, start, end, kind="level", sign=1):
    """Настоящий ход по закрытиям. sign — направление заявленного хода (для peak: max при росте, min при падении)."""
    d0, c0 = _at_or_before(dates, closes, start)
    d1, c1 = _at_or_before(dates, closes, end)
    if c0 is None or c1 is None or not c0 or d1 is None or d1 <= d0:
        return None
    out = {"начало": [d0, round(c0, 4)], "конец": [d1, round(c1, 4)], "уровень": round(c1 / c0 - 1, 4)}
    i0 = bisect.bisect_right(dates, d0)
    i1 = bisect.bisect_right(dates, d1)
    win = list(zip(dates[i0:i1], closes[i0:i1]))
    if win:
        ext = max(win, key=lambda x: x[1]) if sign >= 0 else min(win, key=lambda x: x[1])
        out["экстремум"] = [ext[0], round(ext[1], 4)]
        out["ход_до_экстремума"] = round(ext[1] / c0 - 1, 4)
    out["вид"] = kind
    out["ход"] = out.get("ход_до_экстремума") if kind == "peak" and "ход_до_экстремума" in out else out["уровень"]
    return out


def verdict(claimed_pct, actual):
    """claimed_pct — проценты (+90.5); actual — доля (0.88). → (вердикт, отношение|None)."""
    if actual is None:
        return "нет данных", None
    if claimed_pct is None:
        return "ход посчитан, заявленного числа нет", None
    c = float(claimed_pct) / 100.0
    a = float(actual)
    if c == 0:
        return "ход посчитан, заявленного числа нет", None
    ratio = a / c
    if ratio <= 0:
        return "не подтверждён", round(ratio, 3)
    if RATIO_LO <= ratio <= RATIO_HI:
        return "подтверждён", round(ratio, 3)
    return "по знаку", round(ratio, 3)


def check(ann, observed, series):
    """Одна разметка. ann: {asset, start, end, claimed_pct, quote, kind}; observed — текст прецедента;
    series — (dates, closes) или None. Цитата обязана быть в тексте — иначе разметка отвергнута (П8)."""
    claimed = ann.get("claimed_pct")
    quote = ann.get("quote")
    res = {"asset": ann.get("asset"), "start": ann.get("start"), "end": ann.get("end"), "заявлено_проц": claimed,
           "цитата": quote}
    if claimed is not None and (not quote or str(quote) not in str(observed or "")):
        res.update(вердикт="разметка отвергнута", причина="цитаты заявленного хода нет в тексте прецедента")
        return res
    if not ann.get("asset"):
        res.update(вердикт="нет данных", причина=ann.get("note") or "бумага не размечена")
        return res
    if not series:
        res.update(вердикт="нет данных", причина=f"нет истории цен {ann.get('asset')}")
        return res
    sign = -1 if (claimed is not None and float(claimed) < 0) else 1
    end = ann.get("end")
    slack = int(ann.get("slack_days") or 0)
    if slack and end:
        # у валютных пар Yahoo дневной бар датирован началом суток по Гринвичу: событие дня D видно в баре D+1
        i = bisect.bisect_right(series[0], str(end)[:10])
        end = series[0][min(len(series[0]) - 1, i + slack - 1)] if i < len(series[0]) else end
        res["end_с_запасом"] = end
    mv = actual_move(series[0], series[1], ann.get("start"), end, ann.get("kind") or "level", sign)
    if mv is None:
        res.update(вердикт="нет данных", причина="нет закрытий на эти даты")
        return res
    if mv["уровень"] == 0 and mv.get("ход_до_экстремума") in (0, None):
        res.update(вердикт="нет данных", причина="цена в окне не менялась ни дня — ряд мёртвый, не проверка")
        return res
    v, ratio = verdict(claimed, mv["ход"])
    res.update(вердикт=v, отношение=ratio, факт=mv, факт_проц=round(mv["ход"] * 100, 1))
    return res


def summary(results):
    out = {}
    for r in results:
        out[r["вердикт"]] = out.get(r["вердикт"], 0) + 1
    return out
