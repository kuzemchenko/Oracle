# -*- coding: utf-8 -*-
"""mathlib/expectations.py — «что рынок уже ждёт» (решение владельца 07.11, урезанное задание «поиск идей»).

Вопрос продукта: «какое развитие бизнеса предполагает текущая оценка и почему мы ждём другого?»
Прежний ответ «уже в цене» (mathlib/priced_in.py) смотрел только на ПРОЙДЕННЫЙ ход цены — а рост акции не
значит, что эффект отыгран, и отсутствие роста не значит недооценку. Здесь — обратная задача оценки:

  • implied_growth  — какой среднегодовой рост прибыли на N лет вперёд оправдывает текущую капитализацию
                      при ставке дисконтирования r и вечном росте g после горизонта (обратный DCF);
  • implied_revenue_growth — то же через выручку и путь маржи (для убыточных/низкомаржинальных компаний,
                      где прибыль ≤ 0 и метод по прибыли не применим);
  • parse_earnings_trend — консенсус аналитиков EODHD (Earnings::Trend) → периоды, разброс, число
                      аналитиков, пересмотры за 7/30/60/90 дней (только разбор, без сети);
  • compare          — наш сценарий против ожиданий рынка: разрыв, его знак и сила словами.

Детерминированно (Инв#6). Каждый результат несёт ВСЕ входы и допущения (П8: расчёт воспроизводим).
Нет нужного входа или метод не применим — честный отказ с причиной, а не число.

Упрощения (обязаны показываться владельцу вместе с числом):
  • денежный поток владельцам = payout × прибыль (по умолчанию payout=1: вся прибыль доступна владельцам);
  • оценивается капитализация акций (не стоимость компании с долгом) — долг учитывайте через market_cap;
  • рост постоянный N лет, затем вечный g — грубая форма, её цель — порядок величины ожиданий, а не цель цены.
"""
import math

DEFAULT_R = 0.09          # ставка дисконтирования (требуемая доходность акций), допущение
DEFAULT_G = 0.025         # вечный рост после горизонта, допущение
DEFAULT_YEARS = 10
_LO, _HI = -0.5, 2.0      # поиск растёт от −50%/год до +200%/год


def _pv(x, base, r, g, years, payout=1.0, margin_path=None):
    """Приведённая стоимость: base·(1+x)^t (·маржа_t) · payout за годы 1..N + вечный хвост от года N."""
    pv, last = 0.0, 0.0
    for t in range(1, years + 1):
        m = margin_path[t - 1] if margin_path else 1.0
        cf = base * (1.0 + x) ** t * m * payout
        pv += cf / (1.0 + r) ** t
        last = cf
    if years == 0:
        last = base * (margin_path[0] if margin_path else 1.0) * payout
    tv = last * (1.0 + g) / (r - g)
    return pv + tv / (1.0 + r) ** years


def _solve(target, f, lo=_LO, hi=_HI, tol=1e-7, it=200):
    """Корень монотонно возрастающей f(x)=target бисекцией; за пределами — (None, 'ниже'/'выше')."""
    flo, fhi = f(lo), f(hi)
    if target < flo:
        return None, "ниже"
    if target > fhi:
        return None, "выше"
    for _ in range(it):
        mid = (lo + hi) / 2.0
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0, None


def _check_rates(r, g):
    if not (isinstance(r, (int, float)) and isinstance(g, (int, float))) or r <= g:
        return "ставка дисконтирования r должна быть больше вечного роста g"
    return None


def implied_growth(market_cap, base_earnings, r=DEFAULT_R, g=DEFAULT_G, years=DEFAULT_YEARS, payout=1.0):
    """Какой постоянный рост прибыли на `years` лет «зашит» в капитализацию. → dict с входами и итогом."""
    inputs = {"капитализация": market_cap, "прибыль_база": base_earnings, "r": r, "g": g,
              "лет": years, "payout": payout}
    why = _check_rates(r, g)
    if why:
        return {"ok": False, "причина": why, "входы": inputs}
    if not market_cap or market_cap <= 0:
        return {"ok": False, "причина": "нет капитализации", "входы": inputs}
    if base_earnings is None or base_earnings <= 0:
        return {"ok": False, "причина": "прибыль ≤ 0 или нет данных — метод по прибыли не применим, "
                                        "нужен метод по выручке (implied_revenue_growth)", "входы": inputs}
    x, bound = _solve(market_cap, lambda v: _pv(v, base_earnings, r, g, years, payout))
    out = {"ok": x is not None, "входы": inputs, "метод": "обратный DCF по прибыли",
           "pe_сейчас": round(market_cap / base_earnings, 2)}
    if x is None:
        out["причина"] = (f"рынок ждёт рост {'ниже −50' if bound == 'ниже' else 'выше +200'}%/год — "
                          f"за пределами разумной модели")
        out["граница"] = bound
        return out
    out["рост_в_год"] = round(x, 4)
    out["прибыль_через_N"] = round(base_earnings * (1 + x) ** years, 2)
    return out


def implied_revenue_growth(market_cap, revenue, terminal_margin, current_margin=None, r=DEFAULT_R,
                           g=DEFAULT_G, years=DEFAULT_YEARS, payout=1.0):
    """Рост выручки, «зашитый» в цену, при пути маржи от текущей к целевой линейно за `years` лет."""
    cm = terminal_margin if current_margin is None else current_margin
    inputs = {"капитализация": market_cap, "выручка_база": revenue, "маржа_сейчас": cm,
              "маржа_цель": terminal_margin, "r": r, "g": g, "лет": years, "payout": payout}
    why = _check_rates(r, g)
    if why:
        return {"ok": False, "причина": why, "входы": inputs}
    if not market_cap or market_cap <= 0 or not revenue or revenue <= 0:
        return {"ok": False, "причина": "нет капитализации или выручки", "входы": inputs}
    if terminal_margin is None or terminal_margin <= 0:
        return {"ok": False, "причина": "целевая маржа ≤ 0 — бизнес без прибыли в модели не стоит ничего",
                "входы": inputs}
    n = max(1, years)
    path = [cm + (terminal_margin - cm) * t / n for t in range(1, n + 1)]
    x, bound = _solve(market_cap, lambda v: _pv(v, revenue, r, g, n, payout, margin_path=path))
    out = {"ok": x is not None, "входы": inputs, "метод": "обратный DCF по выручке и пути маржи",
           "ps_сейчас": round(market_cap / revenue, 2)}
    if x is None:
        out["причина"] = (f"рынок ждёт рост выручки {'ниже −50' if bound == 'ниже' else 'выше +200'}%/год — "
                          f"за пределами разумной модели")
        out["граница"] = bound
        return out
    out["рост_в_год"] = round(x, 4)
    out["выручка_через_N"] = round(revenue * (1 + x) ** n, 2)
    return out


# ── консенсус аналитиков EODHD (fundamentals → Earnings::Trend) ──────────────────────
_TREND_FIELDS = {
    "eps_avg": "earningsEstimateAvg", "eps_low": "earningsEstimateLow", "eps_high": "earningsEstimateHigh",
    "eps_analysts": "earningsEstimateNumberOfAnalysts", "eps_growth": "earningsEstimateGrowth",
    "rev_avg": "revenueEstimateAvg", "rev_low": "revenueEstimateLow", "rev_high": "revenueEstimateHigh",
    "rev_analysts": "revenueEstimateNumberOfAnalysts", "rev_growth": "revenueEstimateGrowth",
    "eps_now": "epsTrendCurrent", "eps_7d": "epsTrend7daysAgo", "eps_30d": "epsTrend30daysAgo",
    "eps_60d": "epsTrend60daysAgo", "eps_90d": "epsTrend90daysAgo",
    "rev_up_30d": "epsRevisionsUpLast30days", "rev_down_30d": "epsRevisionsDownLast30days",
}


def _f(v):
    try:
        return None if v in (None, "", "NA", "None") else float(v)
    except (TypeError, ValueError):
        return None


def parse_earnings_trend(trend):
    """EODHD Earnings::Trend (словарь по датам или список) → {период: {...}}; периоды «0q», «+1q», «0y», «+1y».
    Поля без данных — None (П8: не 0). Пересмотр за 90 дней = (сейчас − 90 дн. назад)/|90 дн. назад|."""
    rows = trend.values() if isinstance(trend, dict) else (trend or [])
    out = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("period"):
            continue
        rec = {"дата_периода": row.get("date")}
        for k, src in _TREND_FIELDS.items():
            rec[k] = _f(row.get(src))
        now, d90 = rec["eps_now"], rec["eps_90d"]
        rec["пересмотр_90д"] = (round((now - d90) / abs(d90), 4)
                                if now is not None and d90 not in (None, 0) else None)
        if rec["eps_low"] is not None and rec["eps_high"] is not None and rec["eps_avg"]:
            rec["разброс_eps"] = round((rec["eps_high"] - rec["eps_low"]) / abs(rec["eps_avg"]), 4)
        else:
            rec["разброс_eps"] = None
        out[str(row["period"])] = rec
    return out


def consensus_growth(trend_parsed, key="eps_avg"):
    """Рост консенсуса следующего года к текущему (+1y к 0y). Нет данных или база ≤ 0 → None."""
    a = (trend_parsed.get("0y") or {}).get(key)
    b = (trend_parsed.get("+1y") or {}).get(key)
    if a is None or b is None or a <= 0:
        return None
    return round(b / a - 1.0, 4)


# ── наш сценарий против рынка ────────────────────────────────────────────────────────
def compare(ours, market, label="рост в год", tol=0.02):
    """Разрыв между нашим сценарием и ожиданием рынка (оба — доли, напр. 0.15 = 15%).
    tol — порог «совпадает» в процентных пунктах (0.02 = 2 п.п.). → dict со знаком и словами."""
    if ours is None or market is None:
        return {"ok": False, "причина": "нет нашего сценария или ожидания рынка — расхождение не показано",
                "наше": ours, "рынок": market}
    gap = ours - market
    if abs(gap) <= tol:
        word = "совпадает с рынком — расхождения нет, идея держится не на ожиданиях"
    elif gap > 0:
        word = "ждём БОЛЬШЕ рынка"
    else:
        word = "ждём МЕНЬШЕ рынка"
    return {"ok": True, "наше": ours, "рынок": market, "разрыв_пп": round(gap * 100, 2), "вывод": word,
            "показатель": label}


def years_to_justify(market_cap, base_earnings, our_growth, r=DEFAULT_R, g=DEFAULT_G, max_years=40, payout=1.0):
    """Сколько лет нашего роста нужно, чтобы оправдать текущую цену (меньше — цена дешевле нашего сценария)."""
    if not market_cap or base_earnings is None or base_earnings <= 0 or our_growth is None or r <= g:
        return None
    for n in range(0, max_years + 1):
        if _pv(our_growth, base_earnings, r, g, n, payout) >= market_cap:
            return n
    return None


def pct(x):
    """Доля → «+12,3%» для текста (None → «нет данных»)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "нет данных"
    return f"{x * 100:+.1f}%".replace(".", ",")
