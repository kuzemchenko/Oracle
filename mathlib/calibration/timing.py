# -*- coding: utf-8 -*-
"""mathlib/calibration/timing.py — калибровка порогов тайминг-детектора (§23.1 п.1, §4).

§23.1 п.1: «калибровка "1.5σ" и порогов объёма на истории — при каком пройденном ходе
продолжение в среднем ещё было/уже не было». Идея 1-го порядка «мертва», когда дальнейшее
продолжение в сторону уже пройденного хода в среднем перестаёт быть положительным.

Метод (детерминированный, walk-forward легален — §23.1):
  событие в день t: пройденный ход за k дней = log(C[t]/C[t-k]); в σ — делим на σ_дн·√k,
  где σ_дн оценена по ТРЕЙЛИНГ-окну (без заглядывания вперёд). Продолжение = знак(ход)·
  форвардная h-дневная log-доходность. Порог = наименьший |spent_σ|, выше которого среднее
  продолжение ≤ 0. Аналогично для z-всплеска объёма («толпа вошла»).

ЧЕГО НЕТ В ДАННЫХ (П8): открытый интерес (OI) и подразумеваемая волатильность опционов (IV)
в дневном фиде EODHD отсутствуют → их пороги остаются null с пометкой «нет данных».
"""
import numpy as np


def build_events(series, k=20, h=10, vol_window=60):
    """Таблица событий тайминга для одного инструмента.

    Возвращает dict массивов одинаковой длины:
      idx         — индекс дня t в исходном ряду
      spent_sigma — пройденный k-дневный ход в единицах σ_дн·√k (со знаком)
      vol_z       — z-оценка объёма дня t по трейлинг-окну
      cont        — продолжение: знак(ход)·(log C[t+h]-log C[t])
    """
    c = series.close.astype(float)
    v = series.volume.astype(float)
    n = c.size
    start = max(k, vol_window)
    idx, spent_sigma, vol_z, cont = [], [], [], []
    logc = np.full(n, np.nan)
    pos = c > 0
    logc[pos] = np.log(c[pos])
    for t in range(start, n - h):
        if not (np.isfinite(logc[t]) and np.isfinite(logc[t - k]) and np.isfinite(logc[t + h])):
            continue
        spent = logc[t] - logc[t - k]
        rwin = np.diff(logc[t - vol_window:t + 1])
        rwin = rwin[np.isfinite(rwin)]
        if rwin.size < vol_window // 2:
            continue
        sd = rwin.std(ddof=1)
        if sd <= 0:
            continue
        ss = spent / (sd * np.sqrt(k))
        fwd = logc[t + h] - logc[t]
        direction = np.sign(spent)
        if direction == 0:
            continue
        # объём
        vw = v[t - vol_window + 1:t + 1]
        vw = vw[np.isfinite(vw) & (vw > 0)]
        vz = np.nan
        if vw.size >= vol_window // 2 and vw.std(ddof=0) > 0:
            vz = (v[t] - vw.mean()) / vw.std(ddof=0)
        idx.append(t)
        spent_sigma.append(ss)
        vol_z.append(vz)
        cont.append(direction * fwd)
    return {
        "idx": np.array(idx, dtype=int),
        "spent_sigma": np.array(spent_sigma, dtype=float),
        "vol_z": np.array(vol_z, dtype=float),
        "cont": np.array(cont, dtype=float),
    }


def death_threshold(magnitude, cont, grid, min_count=30):
    """Наименьший порог из grid, при котором среднее продолжение для событий
    с magnitude >= порога становится ≤ 0 (игра 1-го порядка «мертва»).

    Возвращает (threshold|None, table) — table: список (порог, n, mean_cont).
    """
    m = np.asarray(magnitude, dtype=float)
    c = np.asarray(cont, dtype=float)
    ok = np.isfinite(m) & np.isfinite(c)
    m, c = m[ok], c[ok]
    table, thr = [], None
    for g in grid:
        sel = m >= g
        nsel = int(sel.sum())
        mean_c = float(c[sel].mean()) if nsel >= min_count else float("nan")
        table.append((float(g), nsel, mean_c))
        if thr is None and nsel >= min_count and mean_c <= 0:
            thr = float(g)
    return thr, table


def binned_continuation(magnitude, cont, edges):
    """Среднее продолжение и hit-rate по корзинам magnitude (для отчёта/прозрачности)."""
    m = np.asarray(magnitude, dtype=float)
    c = np.asarray(cont, dtype=float)
    ok = np.isfinite(m) & np.isfinite(c)
    m, c = m[ok], c[ok]
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (m >= lo) & (m < hi)
        n = int(sel.sum())
        out.append({
            "lo": float(lo), "hi": float(hi), "n": n,
            "mean_cont": float(c[sel].mean()) if n else None,
            "hit_rate": float((c[sel] > 0).mean()) if n else None,
        })
    return out


def pooled_death_threshold(events_list, magnitude_key, grid, min_count=200, use_abs=True):
    """Объединённый по инструментам порог: стабильный единый ориентир на 10k+ событий.

    Per-fold оценки на коротких окнах шумны и липнут к краю сетки (среднее продолжение
    рынка ≈ 0). Пул даёт устойчивое число; per-instrument walk-forward отдельно подтверждает
    обобщаемость (test_separation_rate). Возвращает (threshold|None, table, n_total).
    """
    mags, conts = [], []
    for ev in events_list:
        m = ev[magnitude_key]
        mags.append(np.abs(m) if use_abs else m)
        conts.append(ev["cont"])
    mag = np.concatenate(mags) if mags else np.array([])
    cont = np.concatenate(conts) if conts else np.array([])
    thr, table = death_threshold(mag, cont, grid, min_count)
    return thr, table, int(np.isfinite(mag).sum())


def calibrate_walkforward(events, magnitude_key, grid, folds, min_count=30):
    """Walk-forward калибровка порога по событиям.

    На train каждого фолда ищем death_threshold; на test проверяем разделение
    (среднее продолжение выше порога должно быть ≤ среднего ниже порога).
    Возвращает агрегат: median/IQR порога по фолдам и пер-фолд проверку.
    """
    mag = events[magnitude_key]
    cont = events["cont"]
    per_fold = []
    thresholds = []
    for f in folds:
        m_tr, c_tr = mag[f.train], cont[f.train]
        m_te, c_te = mag[f.test], cont[f.test]
        thr, _ = death_threshold(m_tr, c_tr, grid, min_count)
        rec = {"fold": f.fold, "threshold": thr,
               "n_train": int(np.isfinite(m_tr).sum()),
               "n_test": int(np.isfinite(m_te).sum())}
        if thr is not None:
            above = m_te >= thr
            below = m_te < thr
            na, nb = int(above.sum()), int(below.sum())
            rec["test_mean_above"] = float(c_te[above].mean()) if na else None
            rec["test_mean_below"] = float(c_te[below].mean()) if nb else None
            rec["test_separates"] = (
                rec["test_mean_above"] is not None and rec["test_mean_below"] is not None
                and rec["test_mean_above"] <= rec["test_mean_below"])
            thresholds.append(thr)
        per_fold.append(rec)
    agg = {"n_folds": len(folds), "n_with_threshold": len(thresholds)}
    if thresholds:
        ta = np.array(thresholds)
        agg["threshold_median"] = float(np.median(ta))
        agg["threshold_q25"] = float(np.quantile(ta, 0.25))
        agg["threshold_q75"] = float(np.quantile(ta, 0.75))
        seps = [r.get("test_separates") for r in per_fold if r.get("threshold") is not None]
        agg["test_separation_rate"] = float(np.mean([1.0 if s else 0.0 for s in seps])) if seps else None
    return {"aggregate": agg, "folds": per_fold}
