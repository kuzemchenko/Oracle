# -*- coding: utf-8 -*-
"""mathlib/calibration/precursors.py — библиотека предвестников (§23.1 п.3, Историк-2).

§23.1 п.3: «по 30–50 крупнейшим историческим движениям … какие измеримые события им
предшествовали; частота, с которой предвестник реально предшествовал движению vs ложные
срабатывания». Здесь «измеримые события» = ЦЕНОВЫЕ/ОБЪЁМНЫЕ прокси, считаемые из OHLCV
строго НА МОМЕНТ НАЧАЛА движения (без заглядывания внутрь движения). Новостные предвестники
требуют длинной истории новостей, которой нет (1 мес.), — помечаются «нет данных» (П8).

Каждый предвестник оценивается lift'ом: P(предвестник | было большое движение) /
P(предвестник | случайный день). lift>1 — реально опережал; база = частота ложных срабатываний.
"""
import numpy as np
from ..indicators import rsi as _rsi


def _logc(series):
    c = series.close.astype(float)
    return np.log(np.where(c > 0, c, np.nan))


def biggest_moves(series, window=20, top_n=8, min_gap=20, exclude_dates=None):
    """Крупнейшие |движения| за `window` дней с дедупликацией пересечений (жадно).

    exclude_dates: множество дат битых тиков (mathlib.calibration.dataquality). Движение,
    чья дата НАЧАЛА или КОНЦА — битый тик, отбраковывается (его величина искажена тиком);
    причина пишется в возвращаемый список excluded. Возвращает (moves, excluded).
    """
    exclude_dates = exclude_dates or set()
    logc = _logc(series)
    n = logc.size
    moves = []
    for t in range(window, n):
        if np.isfinite(logc[t]) and np.isfinite(logc[t - window]):
            moves.append((t, logc[t] - logc[t - window]))
    moves.sort(key=lambda x: abs(x[1]), reverse=True)
    picked, excluded = [], []
    for t, m in moves:
        if all(abs(t - p[0]) >= min_gap for p in picked):
            sd, ed = str(series.dates[t - window]), str(series.dates[t])
            if sd in exclude_dates or ed in exclude_dates:
                bad = ed if ed in exclude_dates else sd
                excluded.append({"symbol": series.symbol, "start_date": sd, "end_date": ed,
                                 "magnitude_pct": round(float(np.expm1(m) * 100), 2),
                                 "excluded_due_to_bad_tick": bad,
                                 "reason": f"конечная/начальная дата движения — битый тик {bad}; величина искажена, движение исключено из каталога"})
                continue
            picked.append((t, m))
        if len(picked) >= top_n:
            break
    out = []
    for t, m in picked:
        out.append({
            "symbol": series.symbol,
            "start_idx": int(t - window),
            "end_idx": int(t),
            "start_date": str(series.dates[t - window]),
            "end_date": str(series.dates[t]),
            "horizon_days": int(window),
            "magnitude_log": round(float(m), 4),
            "magnitude_pct": round(float(np.expm1(m) * 100), 2),
            "direction": "up" if m > 0 else "down",
        })
    return out, excluded


def precursor_features(series, s, pre_window=20, vol_window=60, dd_window=60):
    """Измеримые ценовые предвестники НА МОМЕНТ начала движения (индекс s).

    Используются только данные до s включительно (никакого заглядывания в движение).
    """
    c = series.close.astype(float)
    v = series.volume.astype(float)
    logc = _logc(series)
    feats = {}
    # пройденный тренд за pre_window перед началом движения
    if s - pre_window >= 0 and np.isfinite(logc[s]) and np.isfinite(logc[s - pre_window]):
        feats["prior_trend_log"] = float(logc[s] - logc[s - pre_window])
    else:
        feats["prior_trend_log"] = None
    # всплеск объёма: средний z объёма за pre_window
    if s - vol_window >= 0:
        vw = v[s - vol_window + 1:s + 1]
        vw = vw[np.isfinite(vw) & (vw > 0)]
        if vw.size >= vol_window // 2 and vw.std(ddof=0) > 0:
            recent = v[s - pre_window + 1:s + 1]
            recent = recent[np.isfinite(recent) & (recent > 0)]
            feats["vol_z_buildup"] = float(((recent - vw.mean()) / vw.std(ddof=0)).mean()) if recent.size else None
        else:
            feats["vol_z_buildup"] = None
    else:
        feats["vol_z_buildup"] = None
    # реализованная волатильность за pre_window (дневная σ log-доходностей)
    if s - pre_window >= 0:
        r = np.diff(logc[s - pre_window:s + 1])
        r = r[np.isfinite(r)]
        feats["realized_vol"] = float(r.std(ddof=1)) if r.size >= 5 else None
    else:
        feats["realized_vol"] = None
    # RSI(14) на момент s
    cc = c[:s + 1]
    cc = cc[np.isfinite(cc) & (cc > 0)]
    if cc.size >= 15:
        feats["rsi14"] = float(_rsi(cc, 14)[-1])
    else:
        feats["rsi14"] = None
    # просадка от пика за dd_window
    if s - dd_window >= 0:
        w = c[s - dd_window:s + 1]
        w = w[np.isfinite(w) & (w > 0)]
        if w.size:
            peak = w.max()
            feats["drawdown_from_peak"] = float((c[s] - peak) / peak) if peak > 0 else None
        else:
            feats["drawdown_from_peak"] = None
    else:
        feats["drawdown_from_peak"] = None
    return feats


# Бинарные предвестники: (имя, функция признака→значение, направление сравнения, читаемое описание)
BINARY_PRECURSORS = {
    "volume_buildup": ("vol_z_buildup", "high", "объём набирал темп перед движением (средний z > порога)"),
    "elevated_volatility": ("realized_vol", "high", "повышенная реализованная волатильность перед движением"),
    "rsi_overbought": ("rsi14", "high", "RSI(14) в зоне перекупленности"),
    "rsi_oversold": ("rsi14", "low", "RSI(14) в зоне перепроданности"),
    "prior_uptrend": ("prior_trend_log", "high", "восходящий тренд перед движением"),
    "deep_drawdown": ("drawdown_from_peak", "low", "глубокая просадка от пика перед движением"),
}


def _thresholds_from_history(series, eligible_idx, pre_window, vol_window, dd_window):
    """Пороги бинаризации признаков — квантили по всем приемлемым дням истории."""
    feats = [precursor_features(series, s, pre_window, vol_window, dd_window) for s in eligible_idx]
    cols = {}
    for key in ("vol_z_buildup", "realized_vol", "rsi14", "prior_trend_log", "drawdown_from_peak"):
        vals = np.array([f[key] for f in feats if f[key] is not None], dtype=float)
        cols[key] = vals
    return cols, feats


def precursor_stats(series, events, pre_window=20, vol_window=60, dd_window=60,
                    hi_q=0.70, lo_q=0.30):
    """Статистика предвестников для одного инструмента: lift и базовая частота.

    lift = freq(предвестник | большое движение) / freq(предвестник | случайный день).
    Базовая частота ≈ доля ложных срабатываний (предвестник был, движения не последовало).
    """
    n = len(series)
    eligible = [s for s in range(max(pre_window, vol_window, dd_window), n)]
    if not eligible or not events:
        return {"insufficient": True}
    cols, _ = _thresholds_from_history(series, eligible, pre_window, vol_window, dd_window)
    cuts = {}
    for key, vals in cols.items():
        if vals.size >= 30:
            cuts[key] = {"hi": float(np.quantile(vals, hi_q)), "lo": float(np.quantile(vals, lo_q))}
    event_starts = [e["start_idx"] for e in events]
    ev_feats = [precursor_features(series, s, pre_window, vol_window, dd_window) for s in event_starts]
    base_feats = [precursor_features(series, s, pre_window, vol_window, dd_window) for s in eligible]

    def present(featlist, key, side):
        if key not in cuts:
            return None
        c = cuts[key]
        ok = [f[key] for f in featlist if f[key] is not None]
        if not ok:
            return None
        ok = np.array(ok, float)
        return float((ok >= c["hi"]).mean()) if side == "high" else float((ok <= c["lo"]).mean())

    stats = {}
    for name, (key, side, desc) in BINARY_PRECURSORS.items():
        fm = present(ev_feats, key, side)
        fb = present(base_feats, key, side)
        if fm is None or fb is None or fb == 0:
            stats[name] = {"insufficient": True, "feature": key, "description": desc}
            continue
        stats[name] = {
            "feature": key, "side": side, "description": desc,
            "freq_before_move": round(fm, 3),
            "base_rate": round(fb, 3),
            "lift": round(fm / fb, 2),
            "n_moves": len(event_starts),
            "n_base_days": len(eligible),
        }
    return stats


def pooled_precursor_stats(series_list, moves_by_symbol, pre_window=20,
                           vol_window=60, dd_window=60, hi_q=0.70, lo_q=0.30):
    """Объединённая статистика предвестников по нескольким инструментам.

    Присутствие признака считается по ПЕР-ИНСТРУМЕНТНЫМ порогам (квантили его истории),
    затем доли усредняются по всем событиям/дням пула — больше движений = устойчивее lift.
    """
    ev_present = {name: [] for name in BINARY_PRECURSORS}
    base_present = {name: [] for name in BINARY_PRECURSORS}
    total_moves, total_days = 0, 0
    for ser in series_list:
        events = moves_by_symbol.get(ser.symbol, [])
        if not events:
            continue
        n = len(ser)
        eligible = list(range(max(pre_window, vol_window, dd_window), n))
        cols, _ = _thresholds_from_history(ser, eligible, pre_window, vol_window, dd_window)
        cuts = {k: {"hi": float(np.quantile(v, hi_q)), "lo": float(np.quantile(v, lo_q))}
                for k, v in cols.items() if v.size >= 30}
        ev_feats = [precursor_features(ser, e["start_idx"], pre_window, vol_window, dd_window)
                    for e in events]
        base_feats = [precursor_features(ser, s, pre_window, vol_window, dd_window) for s in eligible]
        total_moves += len(ev_feats)
        total_days += len(base_feats)
        for name, (key, side, _desc) in BINARY_PRECURSORS.items():
            if key not in cuts:
                continue
            c = cuts[key]
            for fl, store in ((ev_feats, ev_present), (base_feats, base_present)):
                for f in fl:
                    if f[key] is None:
                        continue
                    store[name].append(1.0 if (
                        (side == "high" and f[key] >= c["hi"]) or
                        (side == "low" and f[key] <= c["lo"])) else 0.0)
    out = {}
    for name, (key, side, desc) in BINARY_PRECURSORS.items():
        em, bm = ev_present[name], base_present[name]
        if not em or not bm or np.mean(bm) == 0:
            out[name] = {"insufficient": True, "feature": key, "description": desc}
            continue
        fm, fb = float(np.mean(em)), float(np.mean(bm))
        out[name] = {
            "feature": key, "side": side, "description": desc,
            "freq_before_move": round(fm, 3), "base_rate": round(fb, 3),
            "lift": round(fm / fb, 2), "n_moves": len(em), "n_base_days": len(bm),
        }
    return out, total_moves, total_days
