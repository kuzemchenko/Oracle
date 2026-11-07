# -*- coding: utf-8 -*-
"""mathlib/idea_outcome.py — ТРИ РАЗДЕЛЬНЫХ ИТОГА идеи (аудит 07.11, находка №6).

Инцидент: после замечаний аналитик предлагал перенести проверку FSLR на I квартал 2027 и пересмотреть
стоп MP после падения цены — то есть подвинуть условия так, чтобы исходный прогноз не проиграл. Правило:
исходная карточка оценивается ТОЛЬКО по своим исходным условиям, а три итога не смешиваются:

  1. прогноз цены    — §9: закрытие в дату сверки по ту сторону порога (как сверяет resolve);
  2. исполнимость    — было ли закрытие не хуже границы входа в окне входа (лимит не исполнен → сделки нет);
  3. модельная сделка — от первого исполнения по закрытиям: стоп / цель / крайний срок, % результата.

Пересмотр (ops/idea_revision.py) — НОВАЯ версия со своей датой и своими условиями; её итог считается
этой же функцией отдельно и никогда не подменяет итог исходной карточки. Только закрытия (Инв#6).
"""
import datetime


def _d(s):
    return datetime.date.fromisoformat(str(s)[:10])


def _series(closes):
    return sorted((_d(k), float(v)) for k, v in (closes or {}).items() if v is not None)


def forecast_result(spec9, closes):
    """→ {'status': 'сбылось'|'не сбылось'|'ждёт'|'нет закрытия', 'close', 'date'}."""
    rb = _d(spec9["resolve_by"])
    ser = [(d, c) for d, c in _series(closes) if d <= rb]
    if not ser or ser[-1][0] < rb:
        last = ser[-1][0] if ser else None
        return {"status": "ждёт" if last is None or last < rb else "нет закрытия", "close": None, "date": None}
    d, c = ser[-1]
    thr = float(spec9["threshold"])
    hit = c > thr if str(spec9.get("direction")) == "above" else c < thr
    return {"status": "сбылось" if hit else "не сбылось", "close": c, "date": d.isoformat()}


def entry_fill(card, closes, entry_from, entry_until=None):
    """Первое закрытие не хуже границы входа в окне [entry_from … entry_until]. Без entry_limit —
    вход по первому закрытию окна. → {'filled', 'date', 'price'}."""
    up = str(card.get("direction")) == "вверх"
    lim = card.get("entry_limit")
    lim = float(lim) if lim not in (None, "") else None
    until = _d(entry_until) if entry_until else _d(card["spec9"]["resolve_by"])
    for d, c in _series(closes):
        if d < _d(entry_from) or d > until:
            continue
        if lim is None or (c <= lim if up else c >= lim):
            return {"filled": True, "date": d.isoformat(), "price": c}
    return {"filled": False, "date": None, "price": None}


def trade_result(card, closes, fill):
    """Модельная сделка от исполнения: стоп/цель по закрытиям, иначе выход в дату сверки."""
    if not fill.get("filled"):
        return {"status": "сделки не было", "exit": None, "pnl_pct": None}
    up = str(card.get("direction")) == "вверх"
    tgt, stp = float(card["target_price"]), float(card["stop_price"])
    rb = _d(card["spec9"]["resolve_by"])
    p0 = fill["price"]
    last = None
    for d, c in _series(closes):
        if d <= _d(fill["date"]) or d > rb:
            continue
        last = (d, c)
        if (c < stp) if up else (c > stp):
            return _closed("стоп", d, c, p0, up)
        if (c >= tgt) if up else (c <= tgt):
            return _closed("цель", d, c, p0, up)
    if last and last[0] == rb:
        return _closed("крайний срок", last[0], last[1], p0, up)
    return {"status": "открыта", "exit": None, "pnl_pct": None}


def _closed(why, d, c, p0, up):
    pnl = (c / p0 - 1) * 100 if up else (1 - c / p0) * 100
    return {"status": why, "exit": {"date": d.isoformat(), "price": c}, "pnl_pct": round(pnl, 2)}


def evaluate(card, closes, entry_from, entry_until=None):
    """Три итога одной версии идеи. entry_from — дата, с которой вход разрешён (дата охоты или день
    катализатора — как в карточке); entry_until — последняя дата входа (по умолчанию — сверка)."""
    fill = entry_fill(card, closes, entry_from, entry_until)
    return {"прогноз": forecast_result(card["spec9"], closes),
            "вход": fill,
            "сделка": trade_result(card, closes, fill)}
