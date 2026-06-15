# -*- coding: utf-8 -*-
"""mathlib/cascade.py — ДЕТЕРМИНИРОВАННЫЙ движок каскадной амплитуды (Этап 2 PLAN_cascade_first.md).

Инвариант 6 (CLAUDE.md): математику считает КОД, не LLM. LLM только ПРЕДЛАГАЕТ причинную связь
(узел→узел); силу эффекта меряет здесь детерминированная историческая чувствительность.

Модель узла каскада при шоке первоисточника:
  • sensitivity(node | source) — историческая ЭЛАСТИЧНОСТЬ/БЕТА доходностей узла к доходности
    источника на лаге переноса (OLS на синхронных рядах; лаг из knowledge/causal_links.yaml,
    эмпирически = 0 на дневных ETF — честно, см. empirical_lag_finding).
  • amplitude(node) = beta × shock  (ожидаемое движение узла при шоке источника `shock`).
  • reliability — доля дисперсии узла, объяснённая источником (R²); + значимость переноса по
    Fisher-CI корреляции (CI исключает 0 → перенос установлен).
  • probability — Φ((amplitude − threshold)/σ_h), σ_h = остаточная вола на горизонте. При нулевом
    сносе обобщает gaussian_baseline из orchestrator/calibrate.py (μ=0 ⇒ Φ(−k)).

§9/П16: если истории мало ИЛИ перенос статистически НЕ установлен (CI корреляции включает 0) →
sealable=False с честной причиной (П8). Никаких выдуманных бет — «нет данных» легитимен.
"""
import math

import numpy as np

from mathlib.calibration import causal as CA

MIN_OBS = 60            # минимум синхронных наблюдений для оценки беты (иначе «нет данных»)
WEAK_R2 = 0.10          # ниже — перенос помечается слабым (но не запрещается; решает контур)


def _norm_cdf(x):
    """Стандартный нормальный CDF (как orchestrator/calibrate._norm_cdf — единая формула)."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def log_returns(prices):
    """Логарифмические доходности по ряду цен (adjusted_close)."""
    p = np.asarray(prices, dtype=float)
    p = p[p > 0]
    if p.size < 2:
        return np.array([])
    return np.diff(np.log(p))


def ols_beta(x, y):
    """OLS y на x: y ≈ a + beta·x. Возвращает {beta, intercept, corr, r2, n, resid_std, se_beta}.

    resid_std — std остатков (ddof=2); se_beta — стандартная ошибка наклона. None при вырожденности."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = min(x.size, y.size)
    if n < 3:
        return None
    x, y = x[:n], y[:n]
    vx = float(np.var(x))
    if not (vx > 0):
        return None
    mx, my = float(np.mean(x)), float(np.mean(y))
    cov = float(np.mean((x - mx) * (y - my)))
    beta = cov / vx
    intercept = my - beta * mx
    resid = y - (intercept + beta * x)
    dof = max(n - 2, 1)
    resid_std = float(math.sqrt(float(np.sum(resid ** 2)) / dof))
    sx = float(np.std(x))
    sy = float(np.std(y))
    corr = 0.0 if (sx == 0 or sy == 0) else float(np.corrcoef(x, y)[0, 1])
    ss_x = float(np.sum((x - mx) ** 2))
    se_beta = resid_std / math.sqrt(ss_x) if ss_x > 0 else float("inf")
    return {"beta": beta, "intercept": intercept, "corr": corr, "r2": corr ** 2,
            "n": int(n), "resid_std": resid_std, "se_beta": se_beta}


def _align_lag(source_ret, node_ret, lag):
    """Сдвиг на лаг переноса: node реагирует на source через `lag` торговых дней (lag≥0)."""
    s = np.asarray(source_ret, dtype=float)
    nd = np.asarray(node_ret, dtype=float)
    if lag > 0:
        s, nd = s[:s.size - lag], nd[lag:]
    elif lag < 0:
        s, nd = s[-lag:], nd[:nd.size + lag]
    m = min(s.size, nd.size)
    return s[:m], nd[:m]


def node_sensitivity(source_ret, node_ret, lag=0, min_obs=MIN_OBS):
    """Историческая чувствительность узла к источнику (бета) на лаге переноса.

    Возвращает {beta, corr, r2, n, resid_std, lag, corr_ci95, перенос_установлен} или None («нет
    данных» — мало синхронных наблюдений). перенос_установлен = CI корреляции (Fisher) исключает 0.
    """
    s, nd = _align_lag(source_ret, node_ret, lag)
    if min(s.size, nd.size) < min_obs:
        return None
    fit = ols_beta(s, nd)
    if fit is None:
        return None
    lo, hi = CA._fisher_ci(fit["corr"], fit["n"])
    if lo is None or hi is None:           # вырожденный CI (|corr|→1) → перенос явно установлен
        established = abs(fit["corr"]) >= 0.999
    else:
        established = bool(lo > 0 or hi < 0)   # CI не пересекает 0 → перенос статистически установлен
    ci = [None if lo is None else round(lo, 4), None if hi is None else round(hi, 4)]
    return {"beta": round(fit["beta"], 6), "corr": round(fit["corr"], 4),
            "r2": round(fit["r2"], 4), "n": fit["n"], "resid_std": round(fit["resid_std"], 6),
            "lag": int(lag), "corr_ci95": ci,
            "перенос_установлен": established}


def node_amplitude(beta, shock):
    """Ожидаемое движение узла при шоке источника: amplitude = beta × shock (в долях доходности)."""
    return float(beta) * float(shock)


def node_probability(amplitude, resid_std, horizon_days, threshold=0.0):
    """P(движение узла за горизонт ≥ threshold) при сносе=amplitude и остаточной воле σ_h.

    σ_h = resid_std·√horizon. При amplitude=0 обобщает baseline calibrate (Φ(−threshold/σ_h))."""
    sigma_h = float(resid_std) * math.sqrt(max(int(horizon_days), 1))
    if not (sigma_h > 0):
        return None
    return round(_norm_cdf((float(amplitude) - float(threshold)) / sigma_h), 4)


def node_cascade(source_ret, node_ret, shock, *, horizon_days, threshold=0.0,
                 lag=0, min_obs=MIN_OBS):
    """Полная оценка узла: чувствительность → амплитуда → вероятность, с честным §9/П16-гейтом.

    Возвращает dict с sealable (можно ли запечатывать форвард-прогноз на этот узел) и причиной.
    """
    sens = node_sensitivity(source_ret, node_ret, lag=lag, min_obs=min_obs)
    if sens is None:
        return {"sealable": False, "причина": "нет данных: < минимума синхронной истории (П8)",
                "sensitivity": None}
    amp = node_amplitude(sens["beta"], shock)
    prob = node_probability(amp, sens["resid_std"], horizon_days, threshold)
    weak = sens["r2"] < WEAK_R2
    if not sens["перенос_установлен"]:
        sealable, причина = False, "перенос статистически не установлен (CI корреляции включает 0) — П8"
    elif prob is None:
        sealable, причина = False, "нулевая остаточная вола — вероятность не определена"
    else:
        sealable, причина = True, ("перенос установлен" + (" (слабый R²)" if weak else ""))
    return {
        "sealable": sealable, "причина": причина,
        "sensitivity": sens,
        "shock": round(float(shock), 6),
        "amplitude": round(amp, 6),            # ожидаемое движение узла (доли)
        "reliability_r2": sens["r2"],          # доля дисперсии узла от источника
        "слабый_перенос": weak,
        "probability": prob,
        "horizon_days": int(horizon_days),
        "threshold": float(threshold),
    }


# ── живой загрузчик: шок источника → узлы по синхронным рядам из quotes ──────────────
def cascade_from_quotes(shock_symbol, shock_move, node_symbols, *, horizon_days,
                        db=None, links=None, threshold=0.0, lookback=400, min_obs=MIN_OBS):
    """Боевой расчёт: загрузить синхронные ряды, посчитать узлы каскада при шоке источника.

    links — опц. список связей (knowledge/causal_links.yaml) для выбора лага переноса узла;
    эмпирически лаги = 0 (дневные ETF синхронны), поэтому по умолчанию lag=0 (честно).
    """
    from mathlib.calibration import loader as LD
    syms = [shock_symbol] + list(node_symbols)
    _, series = LD.load_aligned(syms, db=db) if db else LD.load_aligned(syms)
    if shock_symbol not in series or series[shock_symbol].adj.size == 0:
        return {"error": f"нет источника {shock_symbol}", "узлы": []}
    src_ret = log_returns(series[shock_symbol].adj[-lookback:])
    out = []
    for nd in node_symbols:
        if nd not in series or series[nd].adj.size == 0:
            out.append({"узел": nd, "sealable": False, "причина": "нет источника цены (П8)"})
            continue
        node_ret = log_returns(series[nd].adj[-lookback:])
        lag = _lag_for_pair(shock_symbol, nd, links)
        res = node_cascade(src_ret, node_ret, shock_move, horizon_days=horizon_days,
                           threshold=threshold, lag=lag, min_obs=min_obs)
        out.append({"узел": nd, **res})
    out.sort(key=lambda r: abs(r.get("amplitude") or 0) * (r.get("reliability_r2") or 0), reverse=True)
    return {"источник": shock_symbol, "shock": shock_move, "horizon_days": horizon_days, "узлы": out}


def _lag_for_pair(a, b, links):
    """Лаг переноса a→b из библиотеки связей (если есть); иначе 0 (эмпирически дневные ETF синхронны)."""
    if not links:
        return 0
    for ln in links:
        pair = [str(x).upper() for x in (ln.get("pair") or [])]
        if {a.upper(), b.upper()} == set(pair):
            return int(ln.get("lag_days") or 0)
    return 0
