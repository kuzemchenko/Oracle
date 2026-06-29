# -*- coding: utf-8 -*-
"""orchestrator/calibrate.py — калибровочный режим §17.3 / §23.2(в): массовые мелкие недельные
прогнозы БЕЗ ставок для набора статистики Brier (скилл calibrate, MASTER_SPEC §10.9).

Что делает (детерминированно, П16-форвард):
  по каждому активу ядра строит 3 §9-разрешимых НЕДЕЛЬНЫХ прогноза «close через H дней
  ВЫШЕ/НИЖЕ уровня X» на порогах {текущий close, ±0.5σ_H} и запечатывает их (mathlib.seal)
  ДО показа. Это реальные форвард-прогнозы — оцениваются ТОЛЬКО по будущему исходу (§16).

Источник вероятности (ЧЕСТНО помечен в записи, prob_model):
  «gaussian_baseline_realized_vol» — бездрейфовая лог-нормальная модель: P(close≥X)=Φ(−k),
  k = ln(X/px)/σ_H, σ_H = дневная realized vol × √H. base_rate — ЭМПИРИЧЕСКАЯ частота того,
  что H-дневный лог-ретёрн ≥ k·σ_H на истории (не выдумка). Это БАЗОВАЯ ЛИНИЯ, а НЕ
  LLM-edge системы; edge накапливается отдельно (kind=funnel_forward / theme_daily).
  Карта развития: заменить базовую вероятность на LLM-форвард по §23.2(в) (бюджет calibrate=$2,
  20 вызовов уже заложен в limits.yaml) — это апгрейд, не блокер запуска режима.

Запечатывание идёт ТОЛЬКО в боевом режиме (write=True, не mock). mock — дымовой расчёт без seal.
"""
import datetime
import math
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mathlib import sealing as SEAL          # noqa: E402
from mathlib import indicators as IND        # noqa: E402
from mathlib import brier as BR              # noqa: E402
from orchestrator import context as C        # noqa: E402

DB = ROOT / "storage" / "oracle.db"
CORE = C.CORE
H_TRADING_DAYS = 5                            # недельный горизонт §17.3
K_OFFSETS = (0.0, 0.5, -0.5)                  # пороги: текущий close и ±0.5σ_H


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _closes(con, symbol, limit=400):
    rows = con.execute("SELECT date, close FROM quotes WHERE symbol=? ORDER BY date DESC LIMIT ?",
                       (symbol, limit)).fetchall()
    rows = rows[::-1]
    return [(r[0], float(r[1])) for r in rows if r[1] is not None]


def _empirical_base_rate(closes, h, log_move):
    """ЭМПИРИЧЕСКАЯ частота: H-дневный лог-ретёрн ≥ log_move на истории (П8: измерено, не выдумка)."""
    px = [c for _, c in closes]
    hits = tot = 0
    for i in range(len(px) - h):
        if px[i] > 0 and px[i + h] > 0:
            if math.log(px[i + h] / px[i]) >= log_move:
                hits += 1
            tot += 1
    return (hits / tot) if tot else None, tot


def build_calibration_predictions(con, run_id, now_dt=None):
    """Список §9-разрешимых калибровочных прогнозов (ещё НЕ запечатаны)."""
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    cal_days = max(1, math.ceil(H_TRADING_DAYS * 7.0 / 5.0))
    resolve_by = (now_dt + datetime.timedelta(days=cal_days)).date()
    resolve_iso = datetime.datetime(resolve_by.year, resolve_by.month, resolve_by.day,
                                    20, 0, 0, tzinfo=datetime.timezone.utc).isoformat()
    preds = []
    for sym in CORE:
        closes = _closes(con, sym)
        if len(closes) < 60:
            continue
        px = closes[-1][1]
        last_date = closes[-1][0]
        prices = [c for _, c in closes]
        sigma_d = float(IND.realized_vol(prices[-61:]))   # дневная realized vol (60 лог-ретёрнов)
        sigma_h = sigma_d * math.sqrt(H_TRADING_DAYS)
        if not (sigma_h > 0):
            continue
        for k in K_OFFSETS:
            log_move = k * sigma_h
            thr = round(px * math.exp(log_move), 4)
            # бездрейфовая лог-нормаль: P(close ≥ thr) = Φ(−k)
            prob = round(_norm_cdf(-k), 4)
            base_rate, n_windows = _empirical_base_rate(closes, H_TRADING_DAYS, log_move)
            preds.append({
                "kind": "calibration",
                "run_id": run_id,
                "asset": sym,
                "direction": "above",
                "threshold": thr,
                "resolve_by": resolve_iso,
                "price_source": f"EODHD close {sym}",
                "probability": prob,
                "base_rate": (None if base_rate is None else round(base_rate, 4)),
                "prob_model": "gaussian_baseline_realized_vol",
                "k_sigma": k,
                "sigma_h": round(sigma_h, 6),
                "horizon_trading_days": H_TRADING_DAYS,
                "threshold_asof_close_date": last_date,
                "base_rate_n_windows": n_windows,
                "spec_ref": "§17.3 калибровка; §23.2(в); §9 разрешимость; П16 форвард-онли",
            })
    return preds


def _current_brier(kinds=None):
    """Brier по сверенным исходам (journal/outcomes.jsonl, join по hash); иначе None (накапливается).
    kinds=None → все; иначе фильтр по kind. Исходы живут ОТДЕЛЬНО от запечатанных прогнозов (П16).
    F0#6/#1.12: разводим калибровочный трек и денежный edge-трек, чтобы счётчики не расходились с resolve."""
    from orchestrator import resolve as RES
    outs_j = RES.read_outcomes()
    probs, outs = [], []
    for o in outs_j:
        if o.get("outcome") in (0, 1) and o.get("probability") is not None \
                and (kinds is None or o.get("kind") in kinds):
            probs.append(float(o["probability"]))
            outs.append(int(o["outcome"]))
    if not probs:
        return None, 0
    return BR.brier_score(probs, outs), len(probs)


def run_calibrate(mode="auto", write=True, now_dt=None):
    """Калибровочный прогон: сгенерировать + запечатать недельные прогнозы, доложить статистику."""
    run_id = f"calibrate_{(now_dt or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y%m%dT%H%M%SZ')}"
    if not DB.exists():
        return {"run_id": run_id, "ОТКАЗ": "storage/oracle.db отсутствует — нет цен для §9"}
    con = sqlite3.connect(DB)
    try:
        preds = build_calibration_predictions(con, run_id, now_dt=now_dt)
    finally:
        con.close()

    # отфильтровать неразрешимые (страховка §9 — в норме все разрешимы)
    good, bad = [], []
    for p in preds:
        probs = SEAL.validate_resolvable(p)
        (bad if probs else good).append((p, probs))

    sealed = []
    do_seal = write and mode != "mock"
    if do_seal:
        for p, _ in good:
            sealed.append(SEAL.seal(p))

    from orchestrator import resolve as RES
    total_recs = len(SEAL.read_predictions())
    brier, n_resolved = _current_brier(kinds=("calibration",))     # калибровочный трек (свой Brier)
    _, n_edge = _current_brier(kinds=RES.MONEY_EDGE_KINDS)          # §11 гейт — ТОЛЬКО edge (как в resolve)
    gate = 270
    out = {
        "run_id": run_id,
        "mode": mode,
        "сгенерировано": len(preds),
        "разрешимо_§9": len(good),
        "неразрешимо": [{"asset": p["asset"], "проблемы": pr} for p, pr in bad],
        "запечатано": len(sealed) if do_seal else 0,
        "запечатывание": ("ДА (боевой)" if do_seal else "НЕТ (mock/no-write — дымовой расчёт)"),
        "всего_в_журнале": total_recs,
        "разрешено_исходов": n_resolved,                            # калибровочных
        "текущий_brier": (None if brier is None else round(brier, 4)),   # калибровочный трек
        "до_ворот_270": max(0, gate - n_edge),                      # F0#6/#1.12: только edge-исходы → §11
        "честность": ("вероятность = базовая линия (гаусс/realized vol), НЕ LLM-edge; "
                      "edge копится в funnel_forward/theme_daily; апгрейд до LLM-калибровки §23.2(в) — карта"),
        "spec_ref": "§17.3, §23.2(в), §10.9; скилл calibrate",
    }
    return out
