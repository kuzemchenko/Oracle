# -*- coding: utf-8 -*-
"""mathlib/scenario_stats.py — арифметика заготовок сценариев (ревью 14.11: Fable п.1–2; владелец 15.11).

Заготовка = «если случится X — бумага Y». Здесь только числа (Инв#6):
  • episode_moves — ход бумаги в прошлых эпизодах того же сценария (из knowledge/cascade_history.yaml:
    там ход_h — логдоходность от закрытия ДО повода через h торговых дней, посчитанная кодом по ценам);
  • summary — сколько случаев, медиана, разброс, в скольких бумага пошла в нужную сторону; при n < MIN_N —
    честная пометка «случаев мало» (вывод о «типичном» ходе по 2–3 эпизодам не делается);
  • passed — какая доля типичного хода уже пройдена от закрытия до повода к текущей цене;
  • levels — цель и стоп для листа ставки по правилу заготовки.
Ничего не выдумывает: нет данных → None с причиной.
"""
import math
import statistics

HORIZONS = (5, 20, 60)
MIN_N = 3                     # меньше трёх случаев — «случаев мало», медиану показываем, но с оговоркой
STOP_SHARE = 0.5              # стоп по умолчанию: половина типичного хода против нас


def _pct_from_log(x):
    return (math.exp(x) - 1.0) * 100.0


def _base_sym(s):
    return str(s or "").upper().split(".")[0]


def episode_moves(episodes, role_tag=None, symbol=None, horizon=20):
    """episodes — записи cascade_history (с links). Звено подходит, если его role_tag — из role_tag (строка или
    список) ИЛИ его today_symbol совпадает с symbol. Одно звено на эпизод.
    → [{"эпизод", "дата", "ход_проц", "бумага_тогда"}] (простой процент за horizon торговых дней)."""
    if horizon not in HORIZONS:
        raise ValueError(f"горизонт {horizon} не из {HORIZONS}")
    tags = {role_tag} if isinstance(role_tag, str) else set(role_tag or [])
    want = _base_sym(symbol) if symbol else None
    out = []
    for e in episodes or []:
        links = list(e.get("links") or [])
        if want:                                    # своя бумага в эпизоде — раньше похожих по роли
            links.sort(key=lambda lk: _base_sym(lk.get("today_symbol")) != want)
        for link in links:
            if not ((tags and link.get("role_tag") in tags) or (want and _base_sym(link.get("today_symbol")) == want)):
                continue
            p = link.get("prices") or {}
            v = p.get(f"ход_{horizon}")
            if not p.get("есть") or not isinstance(v, (int, float)):
                continue
            out.append({"эпизод": e.get("id"), "дата": e.get("trigger_date"),
                        "ход_проц": round(_pct_from_log(v), 2), "бумага_тогда": p.get("тикер") or link.get("then_symbol")})
            break                                   # одно звено на эпизод
    return out


def summary(moves, direction="up"):
    """→ {n, медиана_проц, мин_проц, макс_проц, в_сторону, мало_случаев} или {n: 0, причина}."""
    xs = [m["ход_проц"] for m in moves or [] if isinstance(m.get("ход_проц"), (int, float))]
    if not xs:
        return {"n": 0, "причина": "нет прошлых случаев с ценами"}
    sign = 1 if direction == "up" else -1
    return {"n": len(xs), "медиана_проц": round(statistics.median(xs), 2), "мин_проц": min(xs), "макс_проц": max(xs),
            "в_сторону": sum(1 for x in xs if sign * x > 0), "мало_случаев": len(xs) < MIN_N}


def passed(base_price, now_price, typical_pct):
    """Доля типичного хода, уже пройденная от закрытия до повода. None — если посчитать нельзя."""
    try:
        b, n, t = float(base_price), float(now_price), float(typical_pct)
    except (TypeError, ValueError):
        return None
    if b <= 0 or t == 0:
        return None
    return round(((n / b - 1.0) * 100.0) / t, 3)


def levels(entry, base_price, typical_pct, direction="up", stop_share=STOP_SHARE):
    """Цель — цена до повода, сдвинутая на типичный ход; стоп — вход минус доля типичного хода против нас.
    → {"цель", "стоп"} или {"причина"} (если цель уже за входом — ход пройден, ставки нет)."""
    try:
        e, b, t = float(entry), float(base_price), abs(float(typical_pct))
    except (TypeError, ValueError):
        return {"причина": "нет цены входа, цены до повода или типичного хода"}
    if e <= 0 or b <= 0 or t == 0:
        return {"причина": "нет цены входа, цены до повода или типичного хода"}
    sign = 1 if direction == "up" else -1
    target = b * (1 + sign * t / 100.0)
    stop = e * (1 - sign * t * stop_share / 100.0)
    if sign * (target - e) <= 0:
        return {"причина": "типичный ход уже пройден — цель не дальше текущей цены", "цель": round(target, 2)}
    return {"цель": round(target, 2), "стоп": round(stop, 2)}
