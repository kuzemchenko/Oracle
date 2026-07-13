# -*- coding: utf-8 -*-
"""mathlib/calibration/tail_df.py — подбор df Стьюдента-t per-instrument (этап Д1, ROADMAP 2026-07).

ФАКТ (SYNC 13.07 §4.2): F2#19 перевёл p-value скана цены/объёма на t-хвосты с df-КОНСТАНТАМИ
(5 доходность / 6 лог-объём / 3 фолбэк), эмпирически не подбиравшимися. С 01.07 после FDR — 0
сигналов 12 дней подряд, включая событийные. Этот модуль подбирает df ЭМПИРИЧЕСКИ по историческим
сканным z каждого инструмента (walk-forward §23.1): train-окно → подбор df по соответствию
хвостовых частот → OOS-проверка (доля |z|>2, |z|>3 против номинала выбранного df).

Все пороги процедуры ЗАФИКСИРОВАНЫ константами ниже ДО прогона сравнения (рамка 3 программы:
перебор порогов до пролезания = манипуляция гейтом). Инструменты без устойчивого пина —
честный фолбэк (П8): пул z всех инструментов, значение тоже из этого модуля, не из головы.

Важно о геометрии z: сканный z (mathlib.indicators.zscore) включает последнюю точку в окно
n=20 → |z| ≤ √(n−1) ≈ 4.36 (алгебраическая граница). Поэтому df подбирается по частотам
умеренных хвостов (1.5–3.0σ), а не по экстремумам, которых у этой статистики не бывает.

Математика — не LLM (Инв#6): только детерминированный код, тесты в mathlib/tests/test_tail_df.py.
"""
import math

import numpy as np

from mathlib import tailprob as TP
from mathlib.calibration import walkforward as wf

# ── Пороги процедуры: зафиксированы 2026-07-13 ДО прогона replay-сравнения (рамка 3) ──
# ПЕРЕСМОТР v2 (13.07, ДО replay-сравнения, по walk-forward-отчёту): критерий устойчивости
# «max/min df по фолдам ≤ 4» заменён OOS-валидацией МЕДИАННОГО df. Обоснование измерением
# (ops/reports/fdr_background/): функция потерь по df плоская в верхней области сетки
# (10↔100 почти неразличимы на train-хвостах), поэтому df-ratio отклонял ШУМ плоской области —
# 208/235 инструментов «нестабильны», при том что их МЕДИАННЫЙ df проходил OOS-проверку хвостовых
# частот в ≥75% фолдов у КАЖДОГО (среднее 97%). Валидируем то значение, которое реально уходит
# в конфиг, — прежний df-ratio остаётся в выдаче информационно.
DF_GRID = (2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0, 30.0, 50.0, 100.0)
FIT_THRESHOLDS = (1.5, 2.0, 2.5, 3.0)   # хвостовые пороги подбора (|z| ≤ ~4.36 — см. докстринг)
OOS_THRESHOLDS = (2.0, 3.0)             # OOS-проверка частот |z|>2, |z|>3 (задание Д1)
TRAIN_SIZE = 250                        # ~1 торговый год обучения
TEST_SIZE = 125                         # ~полгода проверки, непересекающиеся окна
MIN_FOLDS = 2                           # меньше 2 фолдов — устойчивость не проверить
MIN_OOS_OK_FRACTION = 0.5               # пин: МЕДИАННЫЙ df OOS-ok в ≥ половине фолдов (v2)
OOS_ALPHA = 0.05                        # двусторонний точный биномиальный тест частоты хвоста
SCAN_WINDOW = 20                        # окно сканного z (ret_z_20 / vol_z_log_20, context._indicators)


# ── Исторические сканные z (та же формула, что живой скан) ─────────────────────────

def _rolling_incl_z(x, window=SCAN_WINDOW):
    """Скользящий z ПОСЛЕДНЕЙ точки окна (точка ВКЛЮЧЕНА в окно, ddof=0) — точная копия
    mathlib.indicators.zscore, применённая к каждому дню истории. При нулевой σ окна → 0."""
    a = np.asarray(x, dtype=float)
    if a.size < window:
        return np.array([])
    sw = np.lib.stride_tricks.sliding_window_view(a, window)
    mean = sw.mean(axis=1)
    std = sw.std(axis=1)  # ddof=0, как indicators.zscore
    last = sw[:, -1]
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(std == 0, 0.0, (last - mean) / std)
    return z[np.isfinite(z)]


def scan_ret_z_series(adj_close, window=SCAN_WINDOW):
    """Историческая серия ret_z_20: z последней ПРОСТОЙ доходности в окне 20
    (context._indicators: zscore(returns(px), 20); returns — простые, не log)."""
    px = np.asarray(adj_close, dtype=float)
    px = px[np.isfinite(px)]
    if px.size < window + 1:
        return np.array([])
    r = np.diff(px) / px[:-1]
    return _rolling_incl_z(r, window)


def scan_vol_z_series(volume, window=SCAN_WINDOW):
    """Историческая серия vol_z_log_20: z последнего лог-объёма (УРОВНИ, не приросты) в окне 20
    (context._indicators: zscore([log(max(v,1)) …], 20))."""
    v = np.asarray(volume, dtype=float)
    v = np.where(np.isfinite(v), v, 0.0)
    lv = np.log(np.maximum(v, 1.0))
    return _rolling_incl_z(lv, window)


# ── Подбор df по соответствию хвостовых частот ─────────────────────────────────────

def _empirical_sf(z, threshold):
    """Эмпирическая двусторонняя хвостовая частота P(|z| ≥ T) со сглаживанием +0.5/+1
    (без нулей — иначе log-потеря вырождается)."""
    a = np.abs(np.asarray(z, dtype=float))
    n = a.size
    if n == 0:
        raise ValueError("пустая серия z")
    k = int(np.sum(a >= threshold))
    return (k + 0.5) / (n + 1.0)


def fit_df(z, grid=DF_GRID, thresholds=FIT_THRESHOLDS):
    """df* = argmin Σ_T (log emp_sf(T) − log t_sf(T, df))² по сетке.

    Ничья → МЕНЬШИЙ df (тяжелее хвост → больший p → консервативнее для FDR).
    Возвращает (df, loss_по_сетке: dict df→loss)."""
    losses = {}
    emp = {t: _empirical_sf(z, t) for t in thresholds}
    for df in grid:
        loss = 0.0
        for t in thresholds:
            nom = TP.student_t_two_sided_p(t, df)
            loss += (math.log(emp[t]) - math.log(nom)) ** 2
        losses[df] = loss
    best = min(grid, key=lambda d: (round(losses[d], 12), d))  # ничья → меньший df
    return best, losses


# ── Точный биномиальный двусторонний тест частоты хвоста (без scipy) ───────────────

def _binom_pmf_ln(n, k, p):
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_two_sided_p(k, n, p):
    """Двусторонний точный биномиальный p (метод малых pmf: сумма исходов с pmf ≤ pmf(k))."""
    if n <= 0:
        raise ValueError("n должно быть > 0")
    if not 0.0 < p < 1.0:
        return 1.0 if (k == 0 and p <= 0.0) or (k == n and p >= 1.0) else 0.0
    ref = _binom_pmf_ln(n, k, p)
    total = 0.0
    for i in range(n + 1):
        if _binom_pmf_ln(n, i, p) <= ref + 1e-12:
            total += math.exp(_binom_pmf_ln(n, i, p))
    return min(1.0, total)


def oos_tail_check(z_test, df, thresholds=OOS_THRESHOLDS, alpha=OOS_ALPHA):
    """OOS-проверка: эмпирические частоты |z|>T против номинала t(df).

    ok = ни на одном пороге частота не отвергает номинал (точный биномиальный
    двусторонний тест, p ≥ alpha). Возвращает {ok, детали по порогам}."""
    a = np.abs(np.asarray(z_test, dtype=float))
    n = int(a.size)
    detail = {}
    ok = True
    for t in thresholds:
        k = int(np.sum(a >= t))
        nom = TP.student_t_two_sided_p(t, df)
        pv = binom_two_sided_p(k, n, nom) if n > 0 else 0.0
        passed = bool(pv >= alpha)
        ok = ok and passed
        detail[f"|z|>{t}"] = {"n": n, "наблюдено": k, "номинал_p": round(nom, 6),
                              "ожидалось": round(n * nom, 2), "биномиальный_p": round(pv, 4),
                              "ok": passed}
    return {"ok": bool(ok and n > 0), "пороги": detail}


# ── Walk-forward калибровка одного инструмента ─────────────────────────────────────

def _snap_to_grid(value, grid=DF_GRID):
    """Ближайшее значение сетки; при равенстве расстояний — меньший df (консервативнее)."""
    return min(grid, key=lambda d: (abs(d - value), d))


def calibrate_instrument(z, train_size=TRAIN_SIZE, test_size=TEST_SIZE, grid=DF_GRID,
                         fit_thresholds=FIT_THRESHOLDS, oos_thresholds=OOS_THRESHOLDS):
    """Walk-forward подбор df для одной серии сканных z.

    Каждый фолд: fit_df на train (кандидаты) → кандидат-итог = МЕДИАННЫЙ фолдовый df
    (прибит к сетке, ничья → меньший) → OOS-валидация ИМЕННО этого значения на test-окне
    КАЖДОГО фолда (v2 — валидируем то, что уходит в конфиг, см. шапку модуля).
    ПИН: фолдов ≥ MIN_FOLDS И медианный df OOS-ok в ≥ MIN_OOS_OK_FRACTION фолдов.

    Возвращает dict: {pinned, df, folds, reason?, n, ok_fraction, fold_df_ratio(информационно)}.
    Не пинится → pinned=False + причина (П8).
    """
    z = np.asarray(z, dtype=float)
    n = int(z.size)
    out = {"n": n, "train_size": train_size, "test_size": test_size}
    if n < train_size + test_size:
        out.update(pinned=False, df=None, folds=[],
                   reason=f"короткая история: {n} z-наблюдений < train+test={train_size + test_size}")
        return out
    folds = wf.walk_forward(n, train_size, test_size)
    dfs = [fit_df(z[f.train], grid, fit_thresholds)[0] for f in folds]
    df_med = _snap_to_grid(float(np.median(dfs)), grid)
    fold_rows, ok_count = [], 0
    for f, df_f in zip(folds, dfs):
        oos = oos_tail_check(z[f.test], df_med, oos_thresholds)   # v2: проверяем медианный df
        ok_count += int(oos["ok"])
        fold_rows.append({"fold": f.fold, "df_train": df_f, "oos_ok_median_df": oos["ok"],
                          "oos": oos["пороги"]})
    out["folds"] = fold_rows
    if len(folds) < MIN_FOLDS:
        out.update(pinned=False, df=None,
                   reason=f"фолдов {len(folds)} < {MIN_FOLDS} — устойчивость не проверить")
        return out
    ok_frac = ok_count / len(folds)
    out.update(ok_fraction=round(ok_frac, 3),
               fold_df_ratio=round(max(dfs) / min(dfs), 2),   # информационно (v1-критерий)
               df_candidate=df_med)
    if ok_frac < MIN_OOS_OK_FRACTION:
        out.update(pinned=False, df=None,
                   reason=(f"медианный df={df_med} не проходит OOS-проверку хвостовых частот: "
                           f"ok в {ok_count}/{len(folds)} фолдов (< {MIN_OOS_OK_FRACTION:.0%})"))
        return out
    out.update(pinned=True, df=df_med)
    return out


def pooled_fallback_df(z_list, grid=DF_GRID, fit_thresholds=FIT_THRESHOLDS,
                       train_size=TRAIN_SIZE, test_size=TEST_SIZE):
    """Фолбэк для инструментов без устойчивого пина: df по ПУЛУ z всех инструментов
    (конкатенация в детерминированном порядке вызывающего) + walk-forward-самопроверка пула.

    Значение фолбэка — из этого расчёта (отчёт), не из головы (П8)."""
    z_all = np.concatenate([np.asarray(z, dtype=float) for z in z_list]) if z_list else np.array([])
    if z_all.size < train_size + test_size:
        return {"df": None, "n": int(z_all.size),
                "reason": "пул слишком мал для walk-forward — фолбэк не установлен (П8)"}
    df_full, _ = fit_df(z_all, grid, fit_thresholds)
    check = calibrate_instrument(z_all, train_size, test_size, grid, fit_thresholds)
    return {"df": df_full, "n": int(z_all.size),
            "walkforward_check": {"pinned": check.get("pinned"), "df": check.get("df"),
                                  "ok_fraction": check.get("ok_fraction"),
                                  "reason": check.get("reason")}}
