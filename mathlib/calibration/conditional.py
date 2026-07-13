# -*- coding: utf-8 -*-
"""mathlib/calibration/conditional.py — УСЛОВНЫЙ оцениватель переноса (этап Д3 «Поискового движка»,
spec/ROADMAP_2026-07_search_engine.md, §23.1 честная зона walk-forward).

Проблема (SYNC 13.07 §3.3): node_sensitivity — БЕЗУСЛОВНАЯ OLS-бета на дневных закрытиях за годы.
Она усредняет тысячи спокойных дней, в которых переноса нет, — СОБЫТИЙНЫЙ перенос не видит по
построению (боевые r²=0.001–0.23, вердикты суда «перенос отсутствует (r²=0.0)»).

Что меряет этот модуль: перенос источник→цель УСЛОВНО НА ЭПИЗОДЫ ШОКА ИСТОЧНИКА (event-study).

  • Эпизод шока — ЕДИНЫЙ для всех пар порог (решение владельца 13.07, Вопрос 4):
        |shock| ≥ SHOCK_SIGMA_FRAC · σ_ист · √W,   W = окно реакции §R2.1 (5 торговых дней),
    где shock = лог-доходность источника за окно W, σ_ист — трейлинг-σ дневных доходностей
    источника (окно SIGMA_RETURNS, только прошлое — look-ahead нет). Формула СОЗНАТЕЛЬНО
    зеркалит активацию B4 (orchestrator/edge_forward.py: SHOCK_SIGMA_FRAC=0.5, SIGMA_BARS=61
    → 60 доходностей); консистентность закреплена тестом-стражем. Эпизоды не пересекаются
    (после эпизода следующий ищется через W дней — та же логика, что кулдаун B4 против
    серийной псевдорепликации одного шока). Порог зафиксирован ДО оценки целей (рамка 3
    дорожной карты: порог не подбирается до пролезания; в отчёте — проверка устойчивости
    выводов к 0.4σ/0.5σ/0.6σ, НЕ выбор лучшего).

  • Отклик цели на лаге ℓ ∈ [0..MAX_LAG] — лог-доходность цели за окно той же ширины W,
    сдвинутое на ℓ дней (ℓ=0 — синхронное окно: дневные ETF переносят синхронно, факт 04.07).
    Условный gain(ℓ) = OLS-наклон отклика цели на шок источника ПО ЭПИЗОДАМ; CI95 и p —
    t-хвост Стьюдента (mathlib.tailprob, прецедент F2#19: без нормального приближения).
    Эффект-статистика — сравнение знак-выровненного отклика в эпизодах с базовым
    распределением окон цели ВНЕ эпизодов (Welch-z + Cohen d). Всё детерминировано (Инв#6).

  • Walk-forward (§23.1, та же сетка, что sensitivity.py: train=504, test=252, шаг=test —
    непересекающиеся OOS-окна): на train-эпизодах выбирается лаг ℓ* (max |t|) и знак gain;
    на OOS-эпизодах фолда gain на ℓ* обязан подтвердиться (тот же знак, CI95 без нуля).
    Неустойчивое → «не установлено» (П8), без пина.

  • N_эпизодов → ярусы честности (маппинг зафиксирован здесь и в отчёте калибровки,
    ops/reports/d3_conditional/REPORT.md):
        wf_established И N_oos_эпизодов ≥ 30 → "A"-кандидат (прецедент N≥30 §10);
        wf_established И 10 ≤ N_oos < 30   → "B" (перенос виден, выборка мала);
        иначе                               → "C" / «не установлено» (механизм не подтверждён).

Этот модуль НИЧЕГО в боевом пути не переключает: ворота/ранг/seal используют прежние поля
(переключение — этап Э4(в,г)). Потребители: mathlib/cascade.py (аддитивное поле
sensitivity_conditional) и ops/calibrate_conditional.py (калибровочный драйвер).
"""
import math

import numpy as np

from mathlib import tailprob as TP
from mathlib.calibration import walkforward as WF

# ── единый порог эпизода (решение владельца 13.07, Вопрос 4; зеркало B4) ────────────────
EVENT_WINDOW_DAYS = 5        # = mathlib.cascade.EVENT_WINDOW_DAYS (§R2.1) — тест-страж следит
SHOCK_SIGMA_FRAC = 0.5       # = orchestrator.edge_forward.SHOCK_SIGMA_FRAC — тест-страж следит
SIGMA_RETURNS = 60           # трейлинг-σ по 60 доходностям (= SIGMA_BARS 61 бар в edge_forward)
MIN_SIGMA_RETURNS = 30       # меньше — σ не считаем честной, день не рассматриваем (П8)

# ── сетка лагов и walk-forward (та же, что mathlib/calibration/sensitivity.py) ──────────
MAX_LAG = 10                 # L: 0..10 торговых дней (2 недели). Объявлен в отчёте Д3: эмпирические
#                              лаги дневных ETF = 0, событийный перенос длиннее 2 недель на дневных
#                              рядах неотличим от нового события; расширение L — новое решение.
TRAIN = 504
TEST = 252
STEP = TEST
MIN_FOLDS = 3                # как sensitivity.MIN_FOLDS
MIN_EPISODES_FIT = 6         # минимум эпизодов для оценки gain на срезе (train ИЛИ oos)
ESTAB_FRAC_MIN = 0.6         # доля валидных фолдов с OOS-подтверждением (= sensitivity)
P_ESTABLISHED = 0.05         # CI95 без нуля ⇔ p<0.05 (прецедент Fisher-CI95 node_sensitivity)

# ── маппинг N_эпизодов → ярус честности (фиксирован ДО прогона, задокументирован в отчёте) ──
TIER_A_MIN_OOS_EPISODES = 30
TIER_B_MIN_OOS_EPISODES = 10


def _t_ppf_975(df):
    """97.5%-квантиль t(df) бисекцией по tailprob.student_t_two_sided_p (детерминированно,
    без scipy в рантайме — прецедент tailprob). p(t) монотонно убывает по t."""
    if df <= 0:
        return None
    lo, hi = 0.0, 200.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if TP.student_t_two_sided_p(mid, df) > P_ESTABLISHED:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def shock_episodes(source_ret, *, window=EVENT_WINDOW_DAYS, sigma_frac=SHOCK_SIGMA_FRAC,
                   sigma_returns=SIGMA_RETURNS, min_sigma_returns=MIN_SIGMA_RETURNS):
    """Эпизоды шока источника на ряде дневных лог-доходностей.

    Возвращает список {"t", "shock", "sigma", "threshold"}: t — индекс ПОСЛЕДНЕЙ доходности
    окна шока (конец окна W). σ трейлинговая (включая окно шока — как _sigma_daily B4 берёт
    бары по asof включительно), только прошлое. Эпизоды не пересекаются: после найденного
    следующий кандидат — через `window` дней (анти-псевдорепликация, зеркало кулдауна B4).
    """
    r = np.asarray(source_ret, dtype=float)
    n = r.size
    out = []
    t = max(window - 1, min_sigma_returns - 1)
    while t < n:
        seg = r[max(0, t - sigma_returns + 1): t + 1]
        if seg.size >= min_sigma_returns:
            sigma = float(seg.std())          # ddof=0 — как r.std() в edge_forward._sigma_daily
            if sigma > 0:
                shock = float(r[t - window + 1: t + 1].sum())
                thr = sigma_frac * sigma * math.sqrt(window)
                if abs(shock) >= thr:
                    out.append({"t": int(t), "shock": shock, "sigma": sigma,
                                "threshold": thr})
                    t += window               # непересекающиеся эпизоды
                    continue
        t += 1
    return out


def lag_response(target_ret, t, lag, *, window=EVENT_WINDOW_DAYS):
    """Отклик цели на лаге lag: лог-доходность цели за окно W, конец окна = t+lag.
    None — окно выходит за ряд (П8: не измерено, не ноль)."""
    r = np.asarray(target_ret, dtype=float)
    j = t + lag
    if j >= r.size or j - window + 1 < 0:
        return None
    return float(r[j - window + 1: j + 1].sum())


def gain_fit(episodes, target_ret, lag, *, window=EVENT_WINDOW_DAYS,
             min_episodes=MIN_EPISODES_FIT):
    """Условный gain на лаге: OLS отклик_цели ≈ a + gain·shock ПО ЭПИЗОДАМ.

    Возвращает {"lag","n","gain","gain_se","gain_ci95","t_stat","p_value"} или None
    (мало эпизодов с измеримым откликом / вырожденность — П8)."""
    xs, ys = [], []
    for e in episodes:
        resp = lag_response(target_ret, e["t"], lag, window=window)
        if resp is not None:
            xs.append(e["shock"])
            ys.append(resp)
    n = len(xs)
    if n < max(3, min_episodes):
        return None
    from mathlib import cascade as CAS
    fit = CAS.ols_beta(xs, ys)
    if fit is None:
        return None
    se = fit.get("se_beta")
    if se is None or not math.isfinite(se) or se <= 0:
        return None
    df = max(n - 2, 1)
    t_stat = fit["beta"] / se
    p = TP.student_t_two_sided_p(t_stat, df)
    tq = _t_ppf_975(df)
    ci = [fit["beta"] - tq * se, fit["beta"] + tq * se]
    return {"lag": int(lag), "n": n, "gain": round(fit["beta"], 6),
            "gain_se": round(se, 6), "gain_ci95": [round(ci[0], 6), round(ci[1], 6)],
            "t_stat": round(t_stat, 4), "p_value": round(p, 6)}


def baseline_windows(target_ret, episodes, *, window=EVENT_WINDOW_DAYS, max_lag=MAX_LAG):
    """Базовое распределение цели ВНЕ эпизодов: непересекающиеся W-оконные доходности,
    исключая зону влияния каждого эпизода [t−W+1 .. t+max_lag+W]."""
    r = np.asarray(target_ret, dtype=float)
    n = r.size
    excluded = np.zeros(n, dtype=bool)
    for e in episodes:
        lo = max(0, e["t"] - window + 1)
        hi = min(n, e["t"] + max_lag + window + 1)
        excluded[lo:hi] = True
    out = []
    t = window - 1
    while t < n:
        if not excluded[max(0, t - window + 1): t + 1].any():
            out.append(float(r[t - window + 1: t + 1].sum()))
            t += window                        # непересекающиеся окна
        else:
            t += 1
    return out


def effect_stats(episodes, target_ret, lag, *, window=EVENT_WINDOW_DAYS, max_lag=MAX_LAG):
    """Эффект-статистика эпизодов против базы: знак-выровненный отклик в эпизодах
    (resp·sign(shock)) vs распределение W-окон цели вне эпизодов. Welch-z + Glass Δ.

    Д3-ревью (LOW): эпизодный отклик знак-выровнен (·sign(shock)), а сырая baseline — нет.
    При дрейфе цели и односторонних шоках это давало ложный «эффект» (сравнение знак-обработанного
    с необработанным). Фикс: baseline тоже проецируется на ОЖИДАЕМЫЙ знак шоков — mean(base)·s̄,
    где s̄ = средний знак шоков эпизодов. Так дрейф цели вычитается симметрично, а остаётся именно
    условный перенос. Δ — Glass (знаменатель = σ baseline, не пул) → имя glass_delta (не cohen_d).
    """
    ep, signs = [], []
    for e in episodes:
        resp = lag_response(target_ret, e["t"], lag, window=window)
        if resp is not None:
            s = 1.0 if e["shock"] >= 0 else -1.0
            ep.append(resp * s)
            signs.append(s)
    base = baseline_windows(target_ret, episodes, window=window, max_lag=max_lag)
    if len(ep) < 3 or len(base) < 3:
        return None
    ep_a, base_a = np.asarray(ep), np.asarray(base)
    s_bar = float(np.mean(signs)) if signs else 0.0            # ожидаемый знак шоков (для проекции базы)
    m_e = float(ep_a.mean())
    m_b_raw = float(base_a.mean())
    m_b = m_b_raw * s_bar                                      # знак-выровненная база: дрейф цели снят
    v_e = float(ep_a.var(ddof=1)) if ep_a.size > 1 else 0.0
    v_b = float(base_a.var(ddof=1)) if base_a.size > 1 else 0.0
    denom = math.sqrt(v_e / ep_a.size + v_b / base_a.size)
    z = (m_e - m_b) / denom if denom > 0 else None
    d = (m_e - m_b) / math.sqrt(v_b) if v_b > 0 else None
    return {"n_episodes": int(ep_a.size), "n_baseline": int(base_a.size),
            "mean_episode_signed": round(m_e, 6), "mean_baseline_raw": round(m_b_raw, 6),
            "mean_baseline_signaligned": round(m_b, 6), "mean_shock_sign": round(s_bar, 4),
            "welch_z": (None if z is None else round(z, 4)),
            "glass_delta": (None if d is None else round(d, 4))}


def _tier(wf_established, n_oos):
    """Маппинг N_эпизодов(OOS) → ярус честности (см. шапку модуля; фиксирован до прогона)."""
    if wf_established and n_oos >= TIER_A_MIN_OOS_EPISODES:
        return "A"
    if wf_established and n_oos >= TIER_B_MIN_OOS_EPISODES:
        return "B"
    return "C"


def estimate_pair(source_ret, target_ret, *, window=EVENT_WINDOW_DAYS,
                  sigma_frac=SHOCK_SIGMA_FRAC, max_lag=MAX_LAG,
                  train=TRAIN, test=TEST, step=STEP,
                  min_episodes=MIN_EPISODES_FIT, min_folds=MIN_FOLDS):
    """Полная условная оценка пары источник→цель: эпизоды → walk-forward → вердикт.

    Возвращает запись (все числа детерминированы, П8: неустойчиво/мало данных → «не установлено»):
      status/wf_established/tier — вердикт; lag_selected, gain_conditional, gain_ci95_fullsample —
      величины (только при установлении); n_episodes*, folds — провенанс.
    """
    s = np.asarray(source_ret, dtype=float)
    d = np.asarray(target_ret, dtype=float)
    n = int(min(s.size, d.size))
    s, d = s[:n], d[:n]
    base = {"window": int(window), "sigma_frac": float(sigma_frac),
            "lag_window": [0, int(max_lag)], "n_obs": n,
            "wf": f"train={train},test={test},step={step} (как sensitivity.py, §23.1)"}
    if n < train + test:
        return {**base, "status": "не установлено", "wf_established": False, "tier": "C",
                "n_episodes": 0, "n_episodes_oos": 0, "n_folds_valid": 0,
                "lag_selected": None, "gain_conditional": None,
                "провенанс": f"нет данных (П8): история {n} < train+test ({train}+{test})"}

    episodes = shock_episodes(s, window=window, sigma_frac=sigma_frac)
    folds_out = []
    folds = WF.walk_forward(n, train, test, step=step)
    for fd in folds:
        # walk-forward-ЧИСТОТА (Д3-ревью HIGH): эпизод принадлежит срезу, только если ВСЁ окно
        # шока [t−W+1 .. t] И ВСЁ окно отклика (до max_lag: конец t+max_lag) лежат внутри среза.
        # Раньше проверялся лишь конец окна шока (t) — эпизод у test_start тянул shock/отклик из
        # train (in-sample доходности в OOS-подтверждении). Теперь оба конца в срезе (для train и OOS).
        ep_tr = [e for e in episodes
                 if e["t"] - window + 1 >= fd.train_start and e["t"] + max_lag < fd.train_end]
        ep_te = [e for e in episodes
                 if e["t"] - window + 1 >= fd.test_start and e["t"] + max_lag < fd.test_end]
        rec = {"fold": fd.fold, "n_ep_train": len(ep_tr), "n_ep_oos": len(ep_te)}
        if len(ep_tr) < min_episodes or len(ep_te) < min_episodes:
            rec.update({"valid": False, "established": False,
                        "причина": "мало эпизодов train/OOS (П8)"})
            folds_out.append(rec)
            continue
        # выбор лага на TRAIN: максимум |t|; тай-брейк — меньший лаг (детерминированно)
        best = None
        for lag in range(0, max_lag + 1):
            fit = gain_fit(ep_tr, d, lag, window=window, min_episodes=min_episodes)
            if fit is None:
                continue
            if best is None or abs(fit["t_stat"]) > abs(best["t_stat"]):
                best = fit
        if best is None or best["p_value"] >= P_ESTABLISHED:
            rec.update({"valid": True, "established": False,
                        "причина": "train: значимый условный перенос не найден"})
            folds_out.append(rec)
            continue
        oos = gain_fit(ep_te, d, best["lag"], window=window, min_episodes=min_episodes)
        if oos is None:
            rec.update({"valid": True, "established": False, "lag": best["lag"],
                        "gain_train": best["gain"],
                        "причина": "OOS: gain не оценился (мало измеримых откликов, П8)"})
            folds_out.append(rec)
            continue
        ok = (oos["gain"] * best["gain"] > 0) and (oos["p_value"] < P_ESTABLISHED)
        rec.update({"valid": True, "established": bool(ok), "lag": best["lag"],
                    "gain_train": best["gain"], "gain_oos": oos["gain"],
                    "p_oos": oos["p_value"],
                    **({} if ok else
                       {"причина": "OOS не подтвердил (знак/CI95 с нулём)"})})
        folds_out.append(rec)

    valid = [f for f in folds_out if f.get("valid")]
    estab = [f for f in folds_out if f.get("established")]
    # Д3-ревью (LOW): ярус честности опирается на N ПОДТВЕРЖДАЮЩИХ OOS-эпизодов, НЕ на все валидные
    # фолды. Раньше провалившиеся фолды (OOS не подтвердил) раздували N и повышали ярус — это искажало
    # смысл «N эпизодов = опора установленного переноса». Теперь маппинг ярусов считает n_oos_estab.
    n_oos_estab = sum(f["n_ep_oos"] for f in estab)
    n_oos_valid = sum(f["n_ep_oos"] for f in valid)          # диагностика (все валидные фолды)
    out = {**base, "n_episodes": len(episodes), "n_episodes_oos": int(n_oos_estab),
           "n_episodes_oos_valid": int(n_oos_valid),
           "n_folds": len(folds_out), "n_folds_valid": len(valid),
           "n_folds_established": len(estab), "folds": folds_out}
    if len(valid) < min_folds:
        out.update({"status": "не установлено", "wf_established": False, "tier": "C",
                    "lag_selected": None, "gain_conditional": None,
                    "провенанс": f"нет данных (П8): валидных фолдов {len(valid)} < {min_folds} "
                                 f"(эпизодов всего {len(episodes)})"})
        return out
    n_oos_total = n_oos_estab                                # ярус — по подтверждающим фолдам (LOW-фикс)
    estab_frac = len(estab) / len(valid)
    signs = {1 if f["gain_oos"] > 0 else -1 for f in estab}
    sign_consistent = len(signs) == 1
    wf_established = bool(estab_frac >= ESTAB_FRAC_MIN and sign_consistent
                          and len(valid) >= min_folds)
    out.update({"established_frac": round(estab_frac, 3),
                "oos_sign_consistent": sign_consistent})
    if not wf_established:
        out.update({"status": "не установлено", "wf_established": False,
                    "tier": _tier(False, n_oos_total),
                    "lag_selected": None, "gain_conditional": None,
                    "провенанс": ("НЕ УСТАНОВЛЕНО (П8): OOS-подтверждение в "
                                  f"{estab_frac:.0%} валидных фолдов (< {ESTAB_FRAC_MIN:.0%})"
                                  + ("" if sign_consistent else "; знак OOS-gain непостоянен"))})
        return out
    # лаг: мода по установленным фолдам (тай-брейк — меньший лаг); gain: медиана OOS-gain
    # установленных фолдов НА выбранном лаге
    lags = [f["lag"] for f in estab]
    lag_selected = min(sorted(set(lags)), key=lambda x: (-lags.count(x), x))
    gains = sorted(f["gain_oos"] for f in estab if f["lag"] == lag_selected)
    if not gains:                                  # мода без gain — теоретически недостижимо
        gains = sorted(f["gain_oos"] for f in estab)
    gain_med = gains[len(gains) // 2] if len(gains) % 2 else \
        0.5 * (gains[len(gains) // 2 - 1] + gains[len(gains) // 2])
    full = gain_fit(episodes, d, lag_selected, window=window, min_episodes=min_episodes)
    eff = effect_stats(episodes, d, lag_selected, window=window, max_lag=max_lag)
    tier = _tier(True, n_oos_total)
    out.update({
        "status": "установлено", "wf_established": True, "tier": tier,
        "lag_selected": int(lag_selected),
        # gain_conditional = медиана OOS-gain ТОЛЬКО подтвердивших фолдов → УСЛОВЛЕНА успехом
        # (condition-on-success): систематически оптимистичнее безусловной оценки, это диагностика
        # переноса, НЕ несмещённая величина edge (Д3-ревью LOW: декларируем смещение явно).
        "gain_conditional": round(gain_med, 6),
        "gain_bias_note": ("gain_conditional — медиана OOS-gain ПОДТВЕРДИВШИХ фолдов "
                           "(condition-on-success): оптимистично смещена; несмещённый ориентир — "
                           "gain_ci95_fullsample по всем эпизодам ℓ*"),
        "gain_oos_folds": [round(g, 6) for g in gains],
        "gain_ci95_fullsample": (full or {}).get("gain_ci95"),   # описательный CI полного сэмпла на ℓ*
        "gain_fullsample": (full or {}).get("gain"),
        "p_value_fullsample": (full or {}).get("p_value"),
        "effect": eff,
        "провенанс": (f"УСТАНОВЛЕНО walk-forward: OOS-подтверждение в {estab_frac:.0%} из "
                      f"{len(valid)} фолдов, знак согласован; lag*={lag_selected}, "
                      f"gain=медиана OOS подтвердивших {round(gain_med, 4)} (condition-on-success, "
                      f"смещена оптимистично); эпизодов {len(episodes)} "
                      f"(OOS подтвердивших {n_oos_total}) → ярус {tier} "
                      f"(маппинг: A≥{TIER_A_MIN_OOS_EPISODES}, B≥{TIER_B_MIN_OOS_EPISODES} OOS-эпизодов)"),
    })
    return out


def estimate_pair_symbols(source_sym, target_sym, *, db=None, asof=None, **kw):
    """Условная оценка для тикеров — синхронные ряды из oracle.db (как sensitivity.on_the_fly).
    Честный «нет данных» (П8) при отсутствии рядов."""
    from mathlib import cascade as CAS
    from mathlib.calibration import loader as LD
    syms = [source_sym, target_sym]
    _, series = LD.load_aligned(syms, db=db, asof=asof) if db else LD.load_aligned(syms, asof=asof)
    miss = [x for x in syms if x not in series or series[x].adj.size == 0]
    if miss:
        return {"источник": source_sym, "узел": target_sym, "status": "не установлено",
                "wf_established": False, "tier": "C", "n_episodes": 0,
                "gain_conditional": None, "lag_selected": None,
                "провенанс": f"нет данных (П8): нет синхронных рядов для {miss}"}
    ra = CAS.log_returns(series[source_sym].adj)
    rb = CAS.log_returns(series[target_sym].adj)
    return {"источник": source_sym, "узел": target_sym, **estimate_pair(ra, rb, **kw)}
