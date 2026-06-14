# -*- coding: utf-8 -*-
"""mathlib/options.py — детерминированная свёртка опционной цепочки в метрики (§21: считает КОД).

Вход — список контрактов EODHD Unicorn Bay (поля: type, strike, exp_date, volatility(=IV),
open_interest, volume, delta, dte, bid, ask). Выход — компактные метрики, которые реально нужны
агентам (1000+ контрактов в промпт не подаём):
  • atm_iv, iv_term (ближняя vs дальняя экспирация) — тайминг «дорогих ожиданий» (П13);
  • put_call_oi_ratio, put_call_vol_ratio, total_oi — позиционирование/антиманипуляция (§4);
  • iv_skew_25d (put IV − call IV у |delta|≈0.25) — индикатор страха (поведенческий);
  • liquidity (есть ли ликвидный рынок опционов для хеджа) — риск-агент (§8 поле 8).
«Нет данных» честно при пустом/неполном входе (П8): метрика = None.
"""


def _num(x):
    try:
        v = float(x)
        return v if v == v else None  # отсев NaN
    except (TypeError, ValueError):
        return None


def _atm_iv(contracts, spot):
    """IV у страйка ближайшего к споту (среднее call/put), на ближайшей экспирации."""
    if spot is None:
        return None
    exps = sorted({c["exp_date"] for c in contracts if c.get("exp_date")})
    if not exps:
        return None
    near = [c for c in contracts if c.get("exp_date") == exps[0]]
    ivs = []
    best = min(near, key=lambda c: abs((_num(c.get("strike")) or 1e9) - spot), default=None)
    if best is None:
        return None
    target = _num(best.get("strike"))
    for c in near:
        if _num(c.get("strike")) == target:
            iv = _num(c.get("volatility"))
            if iv is not None:
                ivs.append(iv)
    return round(sum(ivs) / len(ivs), 4) if ivs else None


def _iv_at_dte(contracts, spot, dte_target):
    """ATM IV для экспирации с dte, ближайшим к целевому (для term-structure)."""
    if spot is None or not contracts:
        return None
    by_exp = {}
    for c in contracts:
        e = c.get("exp_date")
        if e:
            by_exp.setdefault(e, []).append(c)
    # средний dte по экспирации
    def exp_dte(cs):
        ds = [_num(c.get("dte")) for c in cs if _num(c.get("dte")) is not None]
        return sum(ds) / len(ds) if ds else None
    cand = [(e, cs, exp_dte(cs)) for e, cs in by_exp.items()]
    cand = [(e, cs, d) for e, cs, d in cand if d is not None]
    if not cand:
        return None
    e, cs, _ = min(cand, key=lambda x: abs(x[2] - dte_target))
    return _atm_iv(cs, spot)


def summarize(contracts, spot=None):
    """Свёртка цепочки в метрики. spot — цена базового актива (для ATM/moneyness)."""
    valid = [c for c in (contracts or []) if c.get("type") and c.get("exp_date")]
    if not valid:
        return {"insufficient": True, "n_contracts": 0}

    puts = [c for c in valid if str(c.get("type")).lower().startswith("p")]
    calls = [c for c in valid if str(c.get("type")).lower().startswith("c")]

    def _sum(cs, field):
        return sum((_num(c.get(field)) or 0) for c in cs)

    oi_p, oi_c = _sum(puts, "open_interest"), _sum(calls, "open_interest")
    vol_p, vol_c = _sum(puts, "volume"), _sum(calls, "volume")

    atm = _atm_iv(valid, spot)
    iv_near = _iv_at_dte(valid, spot, 30)
    iv_far = _iv_at_dte(valid, spot, 90)
    iv_term = (round(iv_far - iv_near, 4) if (iv_near is not None and iv_far is not None) else None)

    # skew: IV у |delta|≈0.25 put минус call (ближайшая экспирация)
    exps = sorted({c["exp_date"] for c in valid})
    near = [c for c in valid if c.get("exp_date") == exps[0]] if exps else []
    def _iv_at_delta(cs, target):
        cand = [(abs((_num(c.get("delta")) or 0) - target), _num(c.get("volatility")))
                for c in cs if _num(c.get("volatility")) is not None and _num(c.get("delta")) is not None]
        cand = [(d, iv) for d, iv in cand if iv is not None]
        return min(cand, key=lambda x: x[0])[1] if cand else None
    put_iv_25 = _iv_at_delta([c for c in near if str(c.get("type")).lower().startswith("p")], -0.25)
    call_iv_25 = _iv_at_delta([c for c in near if str(c.get("type")).lower().startswith("c")], 0.25)
    skew = (round(put_iv_25 - call_iv_25, 4) if (put_iv_25 is not None and call_iv_25 is not None) else None)

    return {
        "insufficient": False,
        "n_contracts": len(valid),
        "nearest_expiry": exps[0] if exps else None,
        "atm_iv": atm,                                   # IV «у денег» (ближняя экспирация)
        "iv_term_far_minus_near": iv_term,               # >0: дальняя дороже (контанго волы)
        "iv_skew_25d_put_minus_call": skew,              # >0: путы дороже — индикатор страха
        "total_open_interest": int(oi_p + oi_c),
        "put_call_oi_ratio": round(oi_p / oi_c, 3) if oi_c else None,
        "put_call_vol_ratio": round(vol_p / vol_c, 3) if vol_c else None,
        "total_option_volume": int(vol_p + vol_c),
        "liquid": bool((oi_p + oi_c) > 1000),            # есть ли рынок для хеджа (риск-агент)
    }
