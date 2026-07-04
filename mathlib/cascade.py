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


EVENT_WINDOW_DAYS = 5   # §R2.1: окно РЕАКЦИИ на событие. shock (корня) и realized (терминала) меряются
                        # за ОДНО это окно → «отыграно» = сколько терминал уже отреагировал, как каскад
                        # предсказывает (а не сырое 20д-движение всех драйверов — прошлый баг несоосности).


def window_return(prices, window=EVENT_WINDOW_DAYS):
    """Лог-доходность за окно последних `window` баров (реакция на событие). None — мало истории.
    Выравнивает горизонты: и шок корня, и реализованное терминала считаются за это же окно."""
    p = [float(x) for x in (prices or []) if x is not None and float(x) > 0]
    if len(p) < window + 1:
        return None
    return round(math.log(p[-1] / p[-1 - window]), 6)


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
    se = fit.get("se_beta")
    return {"beta": round(fit["beta"], 6), "corr": round(fit["corr"], 4),
            "r2": round(fit["r2"], 4), "n": fit["n"], "resid_std": round(fit["resid_std"], 6),
            "lag": int(lag), "corr_ci95": ci,
            "se_beta": (None if se is None or math.isinf(se) else round(se, 6)),
            "перенос_установлен": established}


def node_amplitude(beta, shock):
    """Ожидаемое движение узла при шоке источника: amplitude = beta × shock (в долях доходности)."""
    return float(beta) * float(shock)


def node_probability(amplitude, resid_std, horizon_days, threshold=0.0,
                     amplitude_sd=0.0, reliability=None):
    """P(движение узла за горизонт ≥ threshold) при сносе=amplitude и остаточной воле σ_h.

    σ_h = resid_std·√horizon. При amplitude=0 обобщает baseline calibrate (Φ(−threshold/σ_h)).

    amplitude_sd: неопределённость самого сноса (проброс дисперсии по звеньям, compose_chain). Входит
        в полосу: σ_total² = σ_h² + amplitude_sd². Узкое звено с широкой полосой → p ближе к 0.5.
    reliability: r² связи (0..1). Сжимаем p к 0.5 пропорционально надёжности: слабая связь (r²≈0.04)
        НЕ должна давать уверенность 0.99 (П8 — мы не уверены, когда связь статистически пуста)."""
    sigma_h = float(resid_std) * math.sqrt(max(int(horizon_days), 1))
    sigma_total = math.sqrt(sigma_h ** 2 + float(amplitude_sd or 0.0) ** 2)
    if not (sigma_total > 0):
        return None
    p = _norm_cdf((float(amplitude) - float(threshold)) / sigma_total)
    if reliability is not None:
        w = max(0.0, min(1.0, float(reliability)))
        p = 0.5 + (p - 0.5) * w           # надёжность=0 → монетка; =1 → исходная p
    return round(p, 4)


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
    """Лаг переноса a→b из библиотеки связей; иначе 0 (эмпирически дневные ETF синхронны).

    Известный баг (ревью 03-04.07, закрыт ночью 04.07): сравнение {a,b}==set(pair) было
    направленно-агностичным — лаг A→B применялся и к B→A. Теперь:
      • запись с ТОЧНЫМ порядком pair==[a,b] — направленный лаг, берётся первой;
      • запись с directed:true и обратным порядком — НЕ наш лаг (пропуск);
      • ненаправленная запись ({a,b} без directed) — симметричный лаг-величина (знак/направление
        НЕ несёт; факт данных 04.07: библиотека causal_links undirected, эмпирические лаги = 0).
    """
    if not links:
        return 0
    au, bu = str(a).upper(), str(b).upper()
    exact = undirected = None
    for ln in links:                                    # кросс-ревью ночи: directed сканируется ВЕСЬ список
        pair = [str(x).upper() for x in (ln.get("pair") or [])]
        if pair == [au, bu]:
            if ln.get("directed"):
                return int(ln.get("lag_days") or 0)     # направленная a→b — высший приоритет
            if exact is None:
                exact = int(ln.get("lag_days") or 0)    # ненаправленная, но в нашем порядке
        elif not ln.get("directed") and {au, bu} == set(pair) and undirected is None:
            undirected = int(ln.get("lag_days") or 0)   # симметричный фолбэк
    if exact is not None:
        return exact
    return undirected if undirected is not None else 0


# ══════════════════════════════════════════════════════════════════════════════════
# §3c PLAN_cascade_first: ярусы честности звена + многозвенная свёртка с пробросом
# дисперсии + edge=амплитуда−отыгранное. Прогноз неочевидной идеи = условный сценарий
# по звеньям (не одна калиброванная p). Инвариант 6: свёртку считает КОД, LLM лишь
# предлагает структуру цепочки и поставляет факты экспозиции (с источником, П8).
# ══════════════════════════════════════════════════════════════════════════════════

# Надёжность звена ограничена сверху ярусом: B (структурная экспозиция без ценового
# подтверждения) и C (только механизм) не могут притворяться эмпирически установленными.
STRUCTURAL_RELIABILITY_CAP = 0.6
MECHANISM_RELIABILITY_CAP = 0.2

_TIER_RANK = {"A": 0, "B": 1, "C": 2}   # A — самый надёжный, C — самый шаткий


def _lowest_tier(tiers):
    """Наименее надёжный (самый шаткий) ярус в цепочке — несущая слабость цепи."""
    present = [t for t in tiers if t in _TIER_RANK]
    return max(present, key=lambda t: _TIER_RANK[t]) if present else None


def _product_mean_var(means, sds):
    """E и Var произведения НЕЗАВИСИМЫХ факторов с (mean, sd). Точно: Var=Π(μ²+σ²)−(Πμ)²."""
    m = 1.0
    e_sq = 1.0
    for mu, sd in zip(means, sds):
        m *= float(mu)
        e_sq *= (float(mu) ** 2 + float(sd) ** 2)
    return m, max(e_sq - m * m, 0.0)


def link_empirical(source_ret, node_ret, *, lag=0, min_obs=MIN_OBS):
    """Ярус A — звено из истории. gain=бета, gain_sd=se_beta, reliability=R² (если перенос
    установлен, иначе 0). None — нет данных (мало синхронной истории, П8)."""
    sens = node_sensitivity(source_ret, node_ret, lag=lag, min_obs=min_obs)
    if sens is None:
        return None
    rel = sens["r2"] if sens["перенос_установлен"] else 0.0
    return {"tier": "A", "gain": sens["beta"], "gain_sd": sens.get("se_beta") or 0.0,
            "reliability": round(rel, 4), "lag": sens["lag"],
            "established": sens["перенос_установлен"], "r2": sens["r2"],
            "провенанс": f"эмпирическая бета OLS, n={sens['n']}, R²={sens['r2']}, "
                         f"перенос_установлен={sens['перенос_установлен']}"}


def link_structural(exposure, op_leverage, *, lag=0, reliability=None,
                    exposure_sd=0.0, op_leverage_sd=0.0, провенанс=""):
    """Ярус B — структурная экспозиция. gain = exposure × op_leverage (доля выручки/затрат
    от затронутого рынка × операционный рычаг). Входы — ФАКТЫ С ИСТОЧНИКОМ (П8), код только
    свёртывает. reliability ограничена STRUCTURAL_RELIABILITY_CAP (нет ценового подтверждения)."""
    gain, var = _product_mean_var([exposure, op_leverage], [exposure_sd, op_leverage_sd])
    rel = (STRUCTURAL_RELIABILITY_CAP if reliability is None
           else min(float(reliability), STRUCTURAL_RELIABILITY_CAP))
    return {"tier": "B", "gain": round(gain, 6), "gain_sd": round(math.sqrt(var), 6),
            "reliability": round(rel, 4), "lag": int(lag), "established": None,
            "провенанс": провенанс or "структурная экспозиция × оп.рычаг (требует источника, П8)"}


def link_mechanism(prior_gain, *, lag=0, prior_sd=None, reliability=None, провенанс=""):
    """Ярус C — только механизм, данных нет. Низкий приор надёжности + широкая полоса (по
    умолчанию CV≈1). Это звено атакует слепой суд; весь edge на C → research-only."""
    g = float(prior_gain)
    psd = abs(g) if prior_sd is None else float(prior_sd)
    rel = (MECHANISM_RELIABILITY_CAP if reliability is None
           else min(float(reliability), MECHANISM_RELIABILITY_CAP))
    return {"tier": "C", "gain": round(g, 6), "gain_sd": round(psd, 6),
            "reliability": round(rel, 4), "lag": int(lag), "established": False,
            "механизм_только": True,
            "провенанс": провенанс or "механизм-гипотеза, не подтверждён данными (П8) — мишень суда"}


def compose_chain(links, shock0, *, shock0_sd=0.0):
    """Свёртка цепочки звеньев при корневом шоке shock0 (доходность первоисточника).

      amplitude = shock0 × Π gainᵢ ;  дисперсия пробрасывается (независимые факторы) →
      длинная цепь / одно шаткое звено = широкая полоса = честная «низкая уверенность».
      reliability = Π reliabilityᵢ (цепь не крепче произведения звеньев) ;  lag = Σ lagᵢ.

    sealable_path=True только если ВСЕ звенья яруса A и перенос установлен — иначе research-only
    (несущее звено B/C, П16: не в форвард-Brier-трек). Звено=None (нет данных) → путь не сворачивается."""
    if not links:
        return {"sealable_path": False, "причина": "пустая цепочка", "amplitude": None, "links": 0}
    missing = [i for i, l in enumerate(links) if l is None]
    if missing:
        return {"sealable_path": False, "amplitude": None, "reliability": None,
                "lowest_tier": None, "links": len(links),
                "причина": f"звено(я) {missing} без данных — путь не разрешим (П8)"}

    means = [shock0] + [l["gain"] for l in links]
    sds = [shock0_sd] + [l.get("gain_sd", 0.0) for l in links]
    amp_mean, amp_var = _product_mean_var(means, sds)
    amp_sd = math.sqrt(amp_var)

    reliability = 1.0
    for l in links:
        reliability *= float(l.get("reliability", 0.0))
    lag_total = sum(int(l.get("lag", 0)) for l in links)
    tiers = [l["tier"] for l in links]
    lowest = _lowest_tier(tiers)
    all_A = all(l.get("tier") == "A" and l.get("established") for l in links)
    return {
        "amplitude": round(amp_mean, 6),               # ожидаемое полное движение терминала (доли)
        "amplitude_sd": round(amp_sd, 6),
        "amplitude_ci68": [round(amp_mean - amp_sd, 6), round(amp_mean + amp_sd, 6)],
        "reliability": round(reliability, 4),          # P(цепь переносит) — произведение звеньев
        "lag_total": lag_total,                        # суммарное окно входа (торг. дни)
        "tiers": tiers,
        "lowest_tier": lowest,                         # несущая слабость цепи (A<B<C)
        "sealable_path": bool(all_A),
        "причина_seal": ("все звенья A и перенос установлен → калибруемо"
                         if all_A else
                         f"несущее звено яруса {lowest} → research-only (не в Brier-трек, П16)"),
        "links": len(links),
    }


UNPRICED_MIN_AMP = 0.005   # ниже этой |амплитуды| (0.5%) доля «отыграно» НЕ определена: предсказанное
                           # каскадом движение на уровне шума, делить на него = мусор (2890%) — П8.


def cascade_edge(amplitude, realized_move, *, amplitude_sd=0.0):
    """edge = ещё НЕ отыгранная на терминале амплитуда = расчётная амплитуда − уже реализованное
    движение. Лекарство от схлопывания в 1-й порядок: отыгранный узел (BNO после −3%) → edge≈0;
    дальний непрокинутый узел → edge>0.

    unpriced_fraction∈[~−1,1]: 1=ничего не отыграно, 0=всё, <0=переотыграно. None, если |амплитуда| <
    UNPRICED_MIN_AMP — каскад предсказывает движение на уровне шума, доля «отыграно» НЕ ИЗМЕРИМА (П8):
    раньше тут делилось на почти-ноль и выходил абсурд (2890%). edge (направление+величина) считается
    всегда; вырождается только ДОЛЯ."""
    if amplitude is None:
        return None
    if realized_move is None:
        # M11 (ревью 04.07): штатный window_return(None на короткой истории) раньше давал
        # TypeError; «отыграно неизвестно» — это None-результат, не крэш (П8)
        return None
    amp = float(amplitude)
    edge = amp - float(realized_move)
    frac = None if abs(amp) < UNPRICED_MIN_AMP else round(edge / amp, 4)
    return {"edge": round(edge, 6), "edge_sd": round(float(amplitude_sd), 6),
            "amplitude": round(amp, 6), "realized": round(float(realized_move), 6),
            "unpriced_fraction": frac}


def edge_rank_score(edge, reliability):
    """Ранг идеи в потоке = |непрокинутый edge| × надёжность цепи (§3c: не по салиентности кластера)."""
    if edge is None or reliability is None:
        return 0.0
    return round(abs(float(edge)) * float(reliability), 6)
