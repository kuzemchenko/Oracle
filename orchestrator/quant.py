# -*- coding: utf-8 -*-
"""orchestrator/quant.py — КОЛИЧЕСТВЕННАЯ ОЦЕНКА research-идеи (инвариант 6: считает КОД, не LLM).

Нарратив аналитика (analyst.py) объясняет «почему»; этот модуль даёт ЧИСЛА: глубину влияния
(амплитуду §3c от корня каскада), масштаб шума бумаги, тайминг (РАНО/ВОВРЕМЯ/ПОЗДНО по пройденному
ходу в σ), базовую вероятность хода, окно/лаг. История тикера берётся из oracle.db или тянется с
EODHD НА ЛЕТУ (динамический резолв §3c §1). П8: нет синхронных рядов корня → амплитуду честно не
считаем, а не выдумываем.
"""
import math
import datetime

import numpy as np

from mathlib import cascade as CAS

MIN_OBS = 60


def _dated_history(ticker, con, api_key, limit=600):
    """(даты, adjusted_close, источник). Сначала oracle.db, иначе EODHD на лету (П8: пусто → пусто)."""
    if con is not None:
        rows = con.execute(
            "SELECT date, adjusted_close FROM quotes WHERE symbol=? AND adjusted_close IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (ticker, limit)).fetchall()
        if len(rows) >= MIN_OBS:
            rows = rows[::-1]
            return [r[0] for r in rows], [float(r[1]) for r in rows], "БД"
    if api_key:
        try:
            from data import eodhd as E
            today = datetime.date.today()
            data = E.fetch_eod(ticker, api_key,
                               (today - datetime.timedelta(days=1000)).isoformat(), today.isoformat()) or []
            dts = [r["date"] for r in data if r.get("adjusted_close")]
            cls = [float(r["adjusted_close"]) for r in data if r.get("adjusted_close")]
            if len(cls) >= MIN_OBS:
                return dts, cls, "EODHD на лету"
        except Exception:  # noqa: BLE001
            pass
    return [], [], "нет"


def _align(d_root, c_root, d_node, c_node):
    """Выровнять две серии по ОБЩИМ датам → (root_closes, node_closes) одинаковой длины."""
    ir = {d: i for i, d in enumerate(d_root)}
    common = [d for d in d_node if d in ir]
    inode = {d: i for i, d in enumerate(d_node)}
    return (np.array([c_root[ir[d]] for d in common], dtype=float),
            np.array([c_node[inode[d]] for d in common], dtype=float))


def _pick_root(cascade_nodes, target):
    """Корень шока = инструмент узла МИНИМАЛЬНОГО порядка, не равный цели (исток каскада)."""
    for n in sorted(cascade_nodes or [], key=lambda x: x.get("порядок") or x.get("order") or 99):
        for t in (n.get("тикеры") or n.get("instruments") or []):
            if t and t != target:
                return t
    return None


def _timing(spent_sigma):
    a = abs(spent_sigma)
    if a < 0.5:
        return "РАНО", "ход почти не начался — рынок ещё не отыграл, вход ранний"
    if a < 1.5:
        return "ВОВРЕМЯ", "ход идёт и ещё не исчерпан"
    return "ПОЗДНО", "бóльшая часть хода уже пройдена — риск опоздать"


def assess(target, cascade_nodes, horizon_days=5, *, con=None, api_key="", k=10):
    """Количественная оценка цели. Возвращает {измеримо, ...числа...} либо честный отказ (П8)."""
    dts, cls, src = _dated_history(target, con, api_key)
    if len(cls) < MIN_OBS:
        return {"измеримо": False, "тикер": target,
                "причина": f"мало истории ({len(cls)} баров, {src}) — числа не считаем (П8)"}
    rets = CAS.log_returns(cls)
    sigma_d = float(np.std(rets[-60:])) if rets.size >= 60 else float(np.std(rets))
    sigma_h = sigma_d * math.sqrt(horizon_days)
    spent = math.log(cls[-1] / cls[-1 - k]) if (len(cls) > k and cls[-1 - k] > 0) else 0.0
    spent_sigma = spent / (sigma_d * math.sqrt(k)) if sigma_d > 0 else 0.0
    timing, twhy = _timing(spent_sigma)

    # ГЛУБИНА ВЛИЯНИЯ: амплитуда цели к шоку корня каскада (§3c), история — на лету
    amp = reliab = tier = edge = root_used = shock_pct = None
    root = _pick_root(cascade_nodes, target)
    if root:
        rd, rc, _ = _dated_history(root, con, api_key)
        if len(rc) >= MIN_OBS:
            ra, na = _align(rd, rc, dts, cls)
            if min(ra.size, na.size) >= MIN_OBS:
                fit = CAS.ols_beta(CAS.log_returns(ra), CAS.log_returns(na))
                if fit and fit["n"] >= MIN_OBS:
                    # шок корня = НАКОПЛЕННЫЙ k-дневный ход (событие, а не один день), сонаправлен спенту цели
                    shock = (math.log(ra[-1] / ra[-1 - k]) if (ra.size > k and ra[-1 - k] > 0)
                             else float(CAS.log_returns(ra)[-1]))
                    shock_pct = round(shock * 100, 2)
                    amp = fit["beta"] * shock
                    reliab = round(fit["r2"], 3)
                    tier = "A (связь в истории)" if fit["r2"] >= 0.10 else "C (связь слабая/механизм)"
                    edge = amp - spent                          # непрокинутое: расчёт − уже отыгранное целью
                    root_used = root

    # ВЕРОЯТНОСТЬ: шанс хода размера амплитуды за горизонт по бездрейфовой норме (база, без события)
    p_base = None
    if amp is not None and sigma_h > 0:
        p_base = round(CAS._norm_cdf(-abs(amp) / sigma_h) * 100, 0)

    return {
        "измеримо": True, "тикер": target, "источник_истории": src, "горизонт_дней": horizon_days,
        "тайминг": timing, "тайминг_почему": twhy, "spent_sigma": round(spent_sigma, 2),
        "типичный_ход_pct": round(sigma_h * 100, 1),            # ±X% — масштаб обычного шума за горизонт
        "амплитуда_pct": (round(amp * 100, 2) if amp is not None else None),
        "шок_корня_pct": shock_pct,
        "надёжность_связи": reliab, "ярус": tier, "корень_шока": root_used,
        "edge_pct": (round(edge * 100, 2) if edge is not None else None),
        "вероятность_базовая_pct": p_base,
    }
