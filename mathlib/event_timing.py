# -*- coding: utf-8 -*-
"""mathlib/event_timing.py — ВРЕМЯ СОБЫТИЯ и ТОРГОВАЯ СЕССИЯ (аудит 07.11, находка №1).

Инцидент: аналитик назвал падение MP 05.11 «реакцией рынка на отличный отчёт», хотя отчёт вышел
ПОСЛЕ закрытия 05.11 — падение было ДО отчёта, а реакцией на него была сессия 06.11. Корень: в системе
не было детерминированного ответа на вопрос «какую сессию двигает это событие». Модуль даёт его кодом
(Инв#6): по времени публикации (UTC) → сессия Нью-Йорка (с переходом на летнее время), первая сессия,
которая может отреагировать, и ход «до события» против хода «реакции» по закрытиям.

Только стандартная библиотека; праздники биржи — по списку `holidays` (если не передан — только
выходные; честно помечается в выводе).
"""
import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PRE_OPEN = datetime.time(4, 0)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
POST_END = datetime.time(20, 0)

SESSIONS = ("pre_market", "regular", "after_close", "non_trading")
SESSION_RU = {"pre_market": "до открытия", "regular": "во время торгов",
              "after_close": "после закрытия", "non_trading": "в неторговое время"}


def _parse_utc(ts):
    if isinstance(ts, datetime.datetime):
        dt = ts
    else:
        s = str(ts).strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("время события без часового пояса — укажи UTC (…Z или +00:00)")
    return dt.astimezone(datetime.timezone.utc)


def is_trading_day(d, holidays=()):
    return d.weekday() < 5 and d.isoformat() not in set(holidays or ())


def next_trading_day(d, holidays=()):
    d = d + datetime.timedelta(days=1)
    while not is_trading_day(d, holidays):
        d += datetime.timedelta(days=1)
    return d


def prev_trading_day(d, holidays=()):
    d = d - datetime.timedelta(days=1)
    while not is_trading_day(d, holidays):
        d -= datetime.timedelta(days=1)
    return d


def classify(ts_utc, holidays=()):
    """Время события → {'utc', 'ny_local', 'ny_date', 'session', 'reaction_day', 'last_close_before'}.
    reaction_day — первая ОСНОВНАЯ сессия, которая может отреагировать на событие:
      до открытия торгового дня → этот же день; во время торгов → этот же день (частично);
      после закрытия или в выходной → следующий торговый день.
    last_close_before — последнее закрытие, сформированное ДО события (база «до»)."""
    dt = _parse_utc(ts_utc)
    loc = dt.astimezone(NY)
    d, t = loc.date(), loc.time()
    trading = is_trading_day(d, holidays)
    if not trading:
        session = "non_trading"
    elif t < OPEN:
        session = "pre_market"
    elif t < CLOSE:
        session = "regular"
    else:
        session = "after_close"
    if session in ("pre_market", "regular"):
        reaction = d
    else:
        reaction = next_trading_day(d, holidays)
    last_before = prev_trading_day(reaction, holidays) if session != "regular" else prev_trading_day(d, holidays)
    return {"utc": dt.isoformat(), "ny_local": loc.isoformat(), "ny_date": d.isoformat(),
            "tz_offset_h": loc.utcoffset().total_seconds() / 3600, "session": session,
            "session_ru": SESSION_RU[session], "reaction_day": reaction.isoformat(),
            "last_close_before": last_before.isoformat(),
            "partial_reaction": session == "regular",
            "holidays_known": bool(holidays)}


def reaction(ts_utc, closes, holidays=()):
    """Ход ДО события и ход РЕАКЦИИ по закрытиям. closes: {YYYY-MM-DD: close}.
    → classify(...) + {'pre_move_pct' (закрытие дня перед базой → база), 'reaction_move_pct'
    (база → закрытие дня реакции), 'base_close', 'reaction_close', 'missing'}. Честные None при дырах."""
    c = classify(ts_utc, holidays)
    cl = {str(k)[:10]: float(v) for k, v in (closes or {}).items() if v is not None}
    base_d = datetime.date.fromisoformat(c["last_close_before"])
    react_d = datetime.date.fromisoformat(c["reaction_day"])
    pre_d = prev_trading_day(base_d, holidays)
    base, react, pre = cl.get(base_d.isoformat()), cl.get(react_d.isoformat()), cl.get(pre_d.isoformat())
    missing = [d.isoformat() for d, v in ((pre_d, pre), (base_d, base), (react_d, react)) if v is None]

    def pct(a, b):
        return round((b / a - 1) * 100, 2) if a and b else None
    c.update({"pre_day": pre_d.isoformat(), "pre_close": pre, "base_close": base, "reaction_close": react,
              "pre_move_pct": pct(pre, base), "reaction_move_pct": pct(base, react), "missing": missing})
    c["text"] = describe(c)
    return c


def describe(c):
    """Одна фраза для человека: что было ДО события и что ПОСЛЕ — без приписывания причины."""
    when = f"{c['ny_date']} {c['session_ru']} (Нью-Йорк, UTC{c['tz_offset_h']:+g})"
    parts = [f"Событие {when}; первая сессия, которая могла на него ответить, — {c['reaction_day']}"
             + (" (событие внутри сессии: часть хода этого дня была ДО него)" if c.get("partial_reaction") else "")]
    if c.get("pre_move_pct") is not None:
        parts.append(f"до события ({c['pre_day']}→{c['last_close_before']}) {c['pre_move_pct']:+.2f}%")
    if c.get("reaction_move_pct") is not None:
        parts.append(f"реакция ({c['last_close_before']}→{c['reaction_day']}) {c['reaction_move_pct']:+.2f}%")
    if c.get("missing"):
        parts.append("нет закрытий: " + ", ".join(c["missing"]))
    if not c.get("holidays_known"):
        parts.append("праздники биржи не учтены")
    return "; ".join(parts) + ". Причинность хода не установлена — это гипотеза, пока нет источника."


def session_matches(declared, ts_utc, holidays=()):
    """Заявленная в карточке сессия против посчитанной кодом. → (ok, посчитанная)."""
    got = classify(ts_utc, holidays)["session"]
    return str(declared or "").strip() == got, got
