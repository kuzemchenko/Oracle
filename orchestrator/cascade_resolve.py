# -*- coding: utf-8 -*-
"""orchestrator/cascade_resolve.py — ДИНАМИЧЕСКИЙ резолв инструмента (Этап 4 PLAN_cascade_first.md).

Последний шаг каскада (§9/П16): узлы движка mathlib/cascade.py (с амплитудой/вероятностью/переносом)
→ либо §9-РАЗРЕШИМЫЙ форвард-прогноз (готов к seal), либо ЛИСТ ОЖИДАНИЯ §17. Брандмауэр развязки:
  • ОТКРЫТИЕ узлов не ограничено списком — инструмент резолвится ДИНАМИЧЕСКИ против quotes
    (U.sealable_universe растёт по мере добора истории), а не из замороженных 14;
  • ЗАПЕЧАТАТЬ можно только при: переносе установлен (Этап 2) И инструмент §9-разрешим
    (U.is_sealable — есть источник цены с историей) И прогноз проходит SEAL.validate_resolvable.
  • Иначе — лист ожидания с честной причиной (П8). Узел без инструмента НЕ выбрасывается.

Направление берёт ЗНАК амплитуды (детерминированно), порог — последний close (как forecast.py),
вероятность — направленная из node_probability. seal В ЖУРНАЛ здесь НЕ делается (только готовит и
валидирует спецификацию) — фактическое запечатывание идёт через forecast.seal_prediction в контуре.
"""
import sqlite3
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import universe_resolver as U     # noqa: E402
from orchestrator import forecast as FC             # noqa: E402
from mathlib import sealing as SEAL                 # noqa: E402

DB = ROOT / "storage" / "oracle.db"


def _latest_close(symbol, con):
    row = con.execute(
        "SELECT date, close FROM quotes WHERE symbol=? AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    return ({"date": row[0], "close": float(row[1])} if row else None)


def _watchlist(symbol, node, reason):
    """Запись листа ожидания §17 (формат, совместимый с ops/bot_watchlist.add_entry)."""
    amp = node.get("amplitude")
    return {
        "kind": "watchlist", "актив": symbol,
        "направление": ("лонг" if (amp or 0) > 0 else "шорт" if (amp or 0) < 0 else None),
        "trigger_text": f"каскадный узел {symbol}: {reason}",
        "amplitude": amp, "причина": reason,
    }


def resolve_node(node, *, run_id, horizon_days, now_dt=None, con=None, db=None):
    """Узел каскада → {kind:'seal', prediction} ИЛИ {kind:'watchlist', ...}. Чистая функция."""
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    symbol = node.get("узел") or node.get("актив")
    if not symbol:
        return _watchlist(symbol, node, "нет символа узла")
    # 1) Перенос установлен? (§9/П16 — Этап 2)
    if not node.get("sealable"):
        return _watchlist(symbol, node, node.get("причина") or "перенос не установлен (П8)")
    # 2) Инструмент §9-разрешим? (брандмауэр Этапа 0, динамически)
    if not U.is_sealable(symbol, con=con, db=db):
        return _watchlist(symbol, node, "нет §9-источника цены / мало истории (П8)")
    # 3) Цена — динамически из quotes
    own = con is None
    if con is None:
        con = sqlite3.connect(str(db or DB), timeout=30)
    try:
        lc = _latest_close(symbol, con)
    finally:
        if own:
            con.close()
    if not lc:
        return _watchlist(symbol, node, "нет close в quotes (П8)")
    # 4) Направление — знак амплитуды; вероятность — направленная
    amp = node.get("amplitude")
    if amp is None or amp == 0:
        return _watchlist(symbol, node, "нулевая/неопределённая амплитуда")
    side = "above" if amp > 0 else "below"
    p_ge0 = node.get("probability")                       # P(доходность ≥ 0 | снос=amp)
    if p_ge0 is None:
        prob = None
    else:
        prob = round(p_ge0 if side == "above" else 1.0 - p_ge0, 4)
    pred = {
        "kind": "cascade_forward", "run_id": run_id,
        "asset": symbol, "direction": side,
        "threshold": round(float(lc["close"]), 4),
        "resolve_by": FC._resolve_by(now_dt, horizon_days),
        "price_source": f"EODHD close {symbol}",
        "probability": prob,
        "amplitude_expected": node.get("amplitude"),
        "reliability_r2": node.get("reliability_r2"),
        "horizon_trading_days": round(float(horizon_days), 2),
        "threshold_asof_close_date": lc["date"],
        "spec_ref": "§9 разрешимость; §5/П5 каскад; П16 форвард-онли; инвариант 6 (амплитуда — код)",
    }
    problems = SEAL.validate_resolvable(pred)
    if problems:
        return _watchlist(symbol, node, "§9: " + "; ".join(problems))
    return {"kind": "seal", "prediction": pred, "узел": symbol}


def seal_spec(fact, *, kind, run_id, horizon_days, con, now_dt=None):
    """B3c (§R3, Вариант 2): §9-спека из узла ВОРОНКИ (факты graph_build.node_to_facts) с меткой
    ТРЕКА kind. БЕЗ ярус-гейта — воронка уже отсеяла по торгуемости/разрешимости; ярус определяет
    куда (kind: cascade_money / cascade_provisional), а не право на seal. Возвращает pred|None.

    Направление = знак неотыгранного edge; порог = последний close; вероятность — направленная."""
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    # F0#3: ЯРУС-ГЕЙТ (defense-in-depth §11/П10) — деньги только для НЕ-research узла (sealable all-A +
    # неотыгранный ход). Если вызывающий просит cascade_money для research-узла — принудительно демотируем.
    # Герметичность money-трека не должна держаться лишь на дисциплине вызывающего.
    if kind == "cascade_money" and fact.get("research"):
        kind = "cascade_provisional"
    symbol = fact.get("symbol")
    amp = fact.get("amplitude")                      # неотыгранный edge
    if not symbol or amp in (None, 0):
        return None
    lc = _latest_close(symbol, con)
    if not lc:
        return None
    side = "above" if amp > 0 else "below"
    p_ge0 = fact.get("probability")
    prob = None if p_ge0 is None else round(p_ge0 if side == "above" else 1.0 - p_ge0, 4)
    pred = {
        "kind": kind, "run_id": run_id, "asset": symbol, "direction": side,
        "threshold": round(float(lc["close"]), 4),
        "resolve_by": FC._resolve_by(now_dt, horizon_days),
        "price_source": f"EODHD close {symbol}",
        "probability": prob,
        "amplitude_expected": amp, "reliability_r2": fact.get("reliability"),
        "ярусы": fact.get("tiers"),
        # рёбра пути для ФОРВАРД-промоушена (forward_promotion): однозвенный путь (len==1) →
        # исход чисто атрибутируется этому ребру; многозвенный — композитный, одному ребру не атрибутируется.
        "cascade_path": fact.get("path_edges") or [],
        "horizon_trading_days": round(float(horizon_days), 2),
        "threshold_asof_close_date": lc["date"],
        "spec_ref": "§9; §5/П5 каскад; П16; B3c трек " + str(kind),
    }
    return pred if not SEAL.validate_resolvable(pred) else None


def resolve_cascade(cascade_result, *, run_id, horizon_days=None, now_dt=None, con=None, db=None):
    """Все узлы каскада → разделение на запечатываемые §9-прогнозы и лист ожидания."""
    hd = horizon_days or cascade_result.get("horizon_days") or 5
    own = con is None
    if con is None and (db or DB.exists()):
        con = sqlite3.connect(str(db or DB), timeout=30)
    try:
        seal, watch = [], []
        for node in cascade_result.get("узлы", []):
            r = resolve_node(node, run_id=run_id, horizon_days=hd, now_dt=now_dt, con=con, db=db)
            (seal if r["kind"] == "seal" else watch).append(r)
    finally:
        if own and con is not None:
            con.close()
    return {
        "источник": cascade_result.get("источник"), "shock": cascade_result.get("shock"),
        "horizon_days": hd,
        "запечатываемо": seal,           # готовые §9-прогнозы (валидны), seal в журнал — контур
        "лист_ожидания": watch,          # узлы без §9-инструмента / без переноса (§17, П8)
        "сводка": f"узлов {len(seal) + len(watch)}: запечатываемо {len(seal)}, в лист ожидания {len(watch)}",
    }
