# -*- coding: utf-8 -*-
"""mathlib/calibration/manipulation.py — калибровка ценовых детекторов манипуляций (§23.1 п.4, §4).

§23.1 п.4: «разметка исторических ложных пробоев / охот за стопами → подбор параметров
детектора с walk-forward-проверкой». Детектор полезен, если его флаг ИНФОРМАТИВЕН:
ложный пробой вверх → форвардное продолжение в среднем не положительное (ловушка),
охота за стопами вниз с быстрым отвержением → форвардное продолжение положительное.

Считаем детерминированно из OHLC. ЧЕГО НЕТ В ДАННЫХ (П8): глубина стакана (для
pump-and-dump малых активов) и потоки крупных трейдеров — соответствующие детекторы
остаются null с пометкой «нет данных».
"""
import numpy as np


def _true_range_atr(high, low, close, n=14):
    """ATR, выровненный по индексу дня (atr[t] — ATR на конец дня t; первые n-1 = nan)."""
    h, l, c = high.astype(float), low.astype(float), close.astype(float)
    N = c.size
    tr = np.empty(N)
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    atr = np.full(N, np.nan)
    if N >= n:
        atr[n - 1] = tr[:n].mean()
        for i in range(n, N):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


# ---------- Ложные пробои (вверх) ----------

def build_breakout_events(series, lookback=20, max_revert=10, h=10):
    """Пробои вверх нового lookback-дневного максимума по close.

    Для каждого пробоя: revert_offset — первый бар (1..max_revert), на котором close
    вернулся НИЖЕ пробитого уровня (None, если не вернулся); fwd — h-дневная форвардная
    log-доходность от дня пробоя (для пробоя вверх ловушка ⇒ fwd ≤ 0).
    """
    c = series.close.astype(float)
    n = c.size
    logc = np.log(np.where(c > 0, c, np.nan))
    idx, revert, fwd = [], [], []
    for t in range(lookback, n - h):
        prior_max = np.nanmax(c[t - lookback:t])
        if not (c[t] > prior_max and np.isfinite(logc[t]) and np.isfinite(logc[t + h])):
            continue
        ro = None
        for j in range(1, max_revert + 1):
            if t + j < n and c[t + j] < prior_max:
                ro = j
                break
        idx.append(t)
        revert.append(ro if ro is not None else -1)   # -1 = не вернулся
        fwd.append(logc[t + h] - logc[t])
    return {"idx": np.array(idx, int), "revert": np.array(revert, int), "fwd": np.array(fwd, float)}


def calibrate_false_breakout(events, revert_grid, min_count=20):
    """Подобрать revert_bars: НАИМЕНЬШЕЕ окно реверта, на котором флаг уже надёжно ловит ловушку.

    Критерий (не монотонный по R, в отличие от «макс. разделения», который всегда тянул бы к
    краю сетки): самый БЫСТРЫЙ R, при котором средняя форвардная доходность помеченных ложными
    пробоев ≤ 0 и ниже, чем у истинных, при достаточном n обеих групп. Экономически: если цена
    вернулась под пробитый уровень за R баров и дальше в среднем идёт вниз — это ловушка.
    Возвращает (best_R|None, table).
    """
    revert = events["revert"]
    fwd = events["fwd"]
    table, best = [], None
    for R in revert_grid:
        false_mask = (revert >= 1) & (revert <= R)
        true_mask = ~false_mask
        nf, nt = int(false_mask.sum()), int(true_mask.sum())
        mf = float(fwd[false_mask].mean()) if nf else float("nan")
        mt = float(fwd[true_mask].mean()) if nt else float("nan")
        sep = (mt - mf) if (nf and nt) else float("nan")
        table.append({"revert_bars": int(R), "n_false": nf, "n_true": nt,
                      "mean_fwd_false": mf, "mean_fwd_true": mt, "separation": sep})
        if best is None and nf >= min_count and nt >= min_count \
                and np.isfinite(mf) and np.isfinite(mt) and mf <= 0 and mf < mt:
            best = int(R)
    return best, table


# ---------- Охота за стопами (пробой опоры вниз с отвержением) ----------

def build_stophunt_events(series, lookback=20, h=10, atr_window=14):
    """Дни, где low пробил lookback-дневный минимум, но close вернулся ВЫШЕ него.

    pen_atr — глубина пробоя опоры в единицах ATR; fwd — h-дневная форвардная доходность
    (после отвержения вниз бычий разворот ⇒ fwd > 0).
    """
    c, h_, l = series.close.astype(float), series.high.astype(float), series.low.astype(float)
    n = c.size
    atr = _true_range_atr(h_, l, c, atr_window)
    logc = np.log(np.where(c > 0, c, np.nan))
    idx, pen_atr, fwd = [], [], []
    for t in range(max(lookback, atr_window), n - h):
        sup = np.nanmin(l[t - lookback:t])
        if not (l[t] < sup and c[t] > sup):
            continue
        if not (np.isfinite(atr[t]) and atr[t] > 0 and np.isfinite(logc[t]) and np.isfinite(logc[t + h])):
            continue
        idx.append(t)
        pen_atr.append((sup - l[t]) / atr[t])
        fwd.append(logc[t + h] - logc[t])
    return {"idx": np.array(idx, int), "pen_atr": np.array(pen_atr, float), "fwd": np.array(fwd, float)}


def calibrate_stop_hunt(events, pen_grid, min_count=20):
    """Подобрать ГРАНИЦУ толерантности глубины пробоя (в ATR), до которой отвержение бычье.

    Критерий: НАИБОЛЬШАЯ глубина p, при которой среди охот с pen_atr ≤ p среднее форвардное
    продолжение ещё > 0 (отскок). Глубже p прокол опоры — уже скорее настоящий пробой, не охота.
    Возвращает (best_pen|None, table). Детектор слабо идентифицируется при малом n — см. провенанс.
    """
    pen = events["pen_atr"]
    fwd = events["fwd"]
    table, best = [], None
    for p in pen_grid:
        sel = (pen > 0) & (pen <= p)
        ns = int(sel.sum())
        mfwd = float(fwd[sel].mean()) if ns >= min_count else float("nan")
        table.append({"max_pen_atr": float(p), "n": ns, "mean_fwd": mfwd})
        if ns >= min_count and np.isfinite(mfwd) and mfwd > 0:
            best = float(p)        # обновляем → останется наибольшее p с бычьим отскоком
    return best, table


def walkforward_param(events, build_calibrator, folds):
    """Прогнать калибратор по walk-forward фолдам индексов событий.

    build_calibrator(train_events) -> (param, _); проверяем устойчивость на test.
    Возвращает агрегат median/IQR параметра и пер-фолд значения.
    """
    keys = [k for k in events if k != "idx"]
    params = []
    per_fold = []
    for f in folds:
        tr = {k: events[k][f.train] for k in events}
        param, _ = build_calibrator(tr)
        per_fold.append({"fold": f.fold, "param": param,
                         "n_train": int(events["idx"][f.train].size)})
        if param is not None:
            params.append(param)
    agg = {"n_folds": len(folds), "n_with_param": len(params)}
    if params:
        pa = np.array(params, dtype=float)
        agg["param_median"] = float(np.median(pa))
        agg["param_q25"] = float(np.quantile(pa, 0.25))
        agg["param_q75"] = float(np.quantile(pa, 0.75))
    return {"aggregate": agg, "folds": per_fold}
