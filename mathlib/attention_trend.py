# -*- coding: utf-8 -*-
"""mathlib/attention_trend.py — растущее внимание толпы по ежедневному ряду (Q-тренды, просьба владельца 09.11).

Владелец: «учитывать не просто новости, но тренды — растёт популярность запроса, начинается движение толпы,
которое может двигать и бумаги». Источник — ежедневные просмотры страницы Википедии (компании или темы):
бесплатно, без лимитов Google Trends, с историей. Здесь — только математика ряда (Инв#6):

  • ускорение = среднее за последние 7 дней / медиана предыдущих 56 дней (не уровень — рост к СВОЕЙ норме);
  • устойчивость = во сколько из последних 7 дней просмотры выше нормы×1.5 (один всплеск ≠ тренд);
  • фаза: тишина / ранний рост / разгон / пик / угасание — по ускорению, его направлению и расстоянию от
    максимума последних 21 дня.

Это мерило внимания, а не прогноз цены: «толпа приходит» иногда двигает бумагу, иногда уже опоздала.
Двигает ли — проверяет только форвард-счёт (ops/attention_watch.py forward), не модель на истории (П16).
"""
import statistics

BASE_DAYS = 56
RECENT_DAYS = 7
PEAK_WINDOW = 21
MIN_BASE_VIEWS = 30          # норма ниже — страница слишком тихая, отношение шумит
RISE, STRONG = 1.5, 3.0      # пороги ускорения: ранний рост / разгон


def trend(views, dates=None):
    """views — ежедневные просмотры по возрастанию дат (≥ BASE_DAYS+RECENT_DAYS). → dict с входами и фазой."""
    v = [float(x or 0) for x in views or []]
    need = BASE_DAYS + RECENT_DAYS
    if len(v) < need:
        return {"ok": False, "причина": f"мало истории: {len(v)} дней < {need}"}
    base = v[-need:-RECENT_DAYS]
    recent = v[-RECENT_DAYS:]
    norm = statistics.median(base)
    if norm < MIN_BASE_VIEWS:
        return {"ok": False, "причина": f"страница тихая: обычно {norm:.0f} просмотров в день (< {MIN_BASE_VIEWS})",
                "норма": norm}
    acc = sum(recent) / len(recent) / norm
    prev = sum(v[-2 * RECENT_DAYS:-RECENT_DAYS]) / RECENT_DAYS / norm
    days_up = sum(1 for x in recent if x > norm * RISE)
    win = v[-PEAK_WINDOW:]
    peak = max(win)
    peak_i = len(win) - 1 - win[::-1].index(peak)
    last3 = sum(v[-3:]) / 3
    from_peak = last3 / peak if peak else 0.0
    if peak >= norm * STRONG and from_peak < 0.5 and peak_i < len(win) - 3:
        phase = "угасание"
    elif acc >= STRONG and from_peak >= 0.8 and days_up >= 4:
        phase = "пик" if acc >= 2 * STRONG else "разгон"
    elif acc >= STRONG:
        phase = "разгон"
    elif acc >= RISE and days_up >= 3 and acc > prev:
        phase = "ранний рост"
    else:
        phase = "тишина"
    out = {"ok": True, "норма_в_день": round(norm, 1), "за_7_дней_в_день": round(sum(recent) / 7, 1),
           "ускорение": round(acc, 2), "ускорение_неделей_раньше": round(prev, 2), "дней_выше_нормы": days_up,
           "максимум_21д": peak, "от_максимума": round(from_peak, 2), "фаза": phase,
           "метод": f"7 дней / медиана {BASE_DAYS} дней до них; пороги ×{RISE} и ×{STRONG}"}
    if dates:
        out["по_дату"] = dates[-1]
        out["день_максимума"] = dates[-PEAK_WINDOW:][peak_i]
    return out


def words(name, res):
    if not res.get("ok"):
        return f"Внимание к «{name}»: не измерено — {res.get('причина')}."
    ph = res["фаза"]
    base = (f"Внимание к «{name}» в Википедии: за неделю в среднем {res['за_7_дней_в_день']:.0f} просмотров в день "
            f"против обычных {res['норма_в_день']:.0f} (×{res['ускорение']:.1f})")
    tail = {"тишина": "толпы нет.",
            "ранний рост": "интерес растёт несколько дней подряд — толпа только начинает приходить.",
            "разгон": "интерес вырос в разы — толпа приходит сейчас.",
            "пик": "интерес на пике — толпа уже здесь, входить поздно чаще, чем рано.",
            "угасание": "пик был недавно и спал — толпа уходит."}[ph]
    return base + f"; фаза «{ph}»: {tail}"


# ── Q-тренды-быстро (09.11): быстрые следы толпы — новости по 15 мин, Википедия по часам, объём торгов ──
MIN_FAST_BASE = 1.0


def surge(recent, history, min_recent=3):
    """Всплеск: сумма за свежее окно против медианы таких же окон в прошлом (тот же размер окна).
    → {"ok", "сейчас", "норма", "кратно"} или отказ. Мало истории или мало событий — честно «не измерено»."""
    if len(history) < 6:
        return {"ok": False, "причина": f"мало истории: {len(history)} окон < 6"}
    norm = statistics.median(history)
    if recent < min_recent:
        return {"ok": True, "сейчас": recent, "норма": norm, "кратно": None, "всплеск": False,
                "причина": f"меньше {min_recent} событий — не всплеск"}
    ratio = recent / max(norm, MIN_FAST_BASE)
    return {"ok": True, "сейчас": recent, "норма": round(norm, 2), "кратно": round(ratio, 2), "всплеск": ratio >= STRONG}


SESSION_MIN = 390          # торговая сессия Нью-Йорка, минут (9:30–16:00)


def volume_pace(volume_so_far, adv, minutes_elapsed):
    """Темп объёма торгов внутри дня: объём к этому часу против обычного дневного объёма × доля прошедшей сессии.
    Утро — самое шумное, поэтому меньше 30 минут не считаем. Линейная доля — упрощение (объём к открытию и
    закрытию выше): темп ×3 и выше — сигнал, ниже — нет."""
    if not adv or adv <= 0 or volume_so_far is None or minutes_elapsed is None:
        return {"ok": False, "причина": "нет обычного объёма или текущего"}
    if minutes_elapsed < 30:
        return {"ok": False, "причина": "первые 30 минут сессии — темп не считаем"}
    frac = min(1.0, minutes_elapsed / SESSION_MIN)
    pace = volume_so_far / (adv * frac)
    return {"ok": True, "темп": round(pace, 2), "объём": volume_so_far, "обычный_день": round(adv), "доля_сессии": round(frac, 3),
            "всплеск": pace >= STRONG}
