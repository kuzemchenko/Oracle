# -*- coding: utf-8 -*-
"""mathlib/sector_rules.py — состояние сектора по ценам его биржевого фонда (Q-вердикт-комитет, п.7 листа). Инв#6.

Владелец 22.11: «оцени не только новости, но и общие возможности — сектор будет расти или корректироваться».
Код НЕ предсказывает сектор: он измеряет, что с ним происходит сейчас, тремя числами:
  • над_средней  — цена фонда против своей средней за 200 торговых дней (долгий тренд);
  • за_3_месяца  — ход фонда за 63 торговых дня и тот же ход широкого рынка (импульс относительно рынка);
  • от_максимума — расстояние до максимума за 252 торговых дня.
Статус (порядок важен):
  1) «перегрет»       — заметно выше средней И у самого годового максимума (покупать поздно — риск отката);
  2) «корректируется» — ниже средней И за 3 месяца в минусе;
  3) «попутный»       — выше средней И за 3 месяца не хуже рынка;
  4) иначе «нейтральный».
Мало данных → None (честное «не измерено», пункт листа не зачитывается).
"""

HOT, CORRECTING, TAILWIND, NEUTRAL = "перегрет", "корректируется", "попутный", "нейтральный"
DEFAULTS = {"дней_средней": 200, "дней_импульса": 63, "дней_максимума": 252,
            "перегрев_над_средней": 0.15, "перегрев_от_максимума": -0.03}


def _vals(closes):
    return [float(c) for _, c in closes or [] if c is not None]


def measures(closes, market_closes=None, rules=None):
    """closes — [(дата, цена)] по возрастанию даты. → {над_средней, за_3_месяца, рынок_за_3_месяца, от_максимума,
    дата} или None, если истории меньше длины долгой средней."""
    r = dict(DEFAULTS, **{k: v for k, v in (rules or {}).items() if k in DEFAULTS})
    v = _vals(closes)
    n_ma, n_mom, n_hi = int(r["дней_средней"]), int(r["дней_импульса"]), int(r["дней_максимума"])
    if len(v) < n_ma or len(v) <= n_mom:
        return None
    last = v[-1]
    ma = sum(v[-n_ma:]) / n_ma
    out = {"над_средней": round(last / ma - 1, 4), "за_3_месяца": round(last / v[-1 - n_mom] - 1, 4),
           "от_максимума": round(last / max(v[-n_hi:]) - 1, 4), "дата": str((closes or [(None,)])[-1][0]),
           "рынок_за_3_месяца": None}
    m = _vals(market_closes)
    if len(m) > n_mom:
        out["рынок_за_3_месяца"] = round(m[-1] / m[-1 - n_mom] - 1, 4)
    return out


def _pct(x):
    """Доля → «+5%» / «−12%» / «0%» (без «-0%» и с настоящим минусом)."""
    n = round(x * 100)
    return "0%" if n == 0 else (f"+{n}%" if n > 0 else f"−{abs(n)}%")


def status(ms, rules=None):
    """ms — результат measures. → (статус | None, [причины словами])."""
    if not ms:
        return None, ["мало истории цен фонда сектора — состояние не измерено"]
    r = dict(DEFAULTS, **{k: v for k, v in (rules or {}).items() if k in DEFAULTS})
    above, mom, dd, mkt = ms["над_средней"], ms["за_3_месяца"], ms["от_максимума"], ms.get("рынок_за_3_месяца")
    where = f"{'выше' if above >= 0 else 'ниже'} своей средней за 200 дней на {abs(above) * 100:.0f}%"
    how = f"за 3 месяца {_pct(mom)}" + (f" (рынок в целом {_pct(mkt)})" if mkt is not None else "")
    if above >= r["перегрев_над_средней"] and dd >= r["перегрев_от_максимума"]:
        return HOT, [where, f"в {abs(dd) * 100:.0f}% от годового максимума", how]
    if above < 0 and mom < 0:
        return CORRECTING, [where, how]
    if above > 0 and (mkt is None or mom >= mkt):
        return TAILWIND, [where, how]
    return NEUTRAL, [where, how]


def against(status_, direction, rules=None):
    """Сектор против идеи этого направления? direction — 'вверх' | 'вниз'. Нет статуса → None (не измерено)."""
    if status_ is None:
        return None
    r = rules or {}
    if direction == "вниз":
        return status_ in (r.get("против_продажи") or [TAILWIND])
    return status_ in (r.get("против_покупки") or [CORRECTING, HOT])
