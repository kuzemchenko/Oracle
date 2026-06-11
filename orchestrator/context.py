# -*- coding: utf-8 -*-
"""orchestrator/context.py — сборка рыночного среза для агентов из РЕАЛЬНЫХ данных.

Источники: storage/oracle.db (quotes, news), config/{universe,thresholds}.yaml,
knowledge/{causal_links,precursors}.yaml. Никаких выдумок — что отсутствует в данных,
помечается «нет данных» прямо в срезе (П8), чтобы агент видел пробел честно.

§5.5: школам подаются ЧАСТИЧНО НЕПЕРЕСЕКАЮЩИЕСЯ срезы (иначе оценки коррелируют и
создают ложную уверенность при синтезе). slice_for() отдаёт каждому агенту его проекцию
полного контекста и рендерит её в user-промпт.

Числовой тулкит теханализа считает КОД (mathlib.indicators), а не LLM — это вход
технического/волнового агентов (инвариант 6 CLAUDE.md, §26 «принципиально не LLM»).
"""
import json
import sqlite3
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "oracle.db"

import sys
sys.path.insert(0, str(ROOT))
from mathlib import indicators as ind  # noqa: E402
from mathlib import waves as wv         # noqa: E402

CORE = ["BNO.US", "USO.US", "SPY.US", "DBC.US", "CPER.US", "COPX.US"]
WAVE_THRESHOLD_PCT = 0.05  # порог ZigZag для разметки волн (§4 «Волновик»); калибруется форвардом


def _load_yaml(rel):
    p = ROOT / rel
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _quotes(con, symbol, limit=260):
    rows = con.execute(
        "SELECT date, open, high, low, close, adjusted_close, volume "
        "FROM quotes WHERE symbol=? ORDER BY date DESC LIMIT ?",
        (symbol, limit)).fetchall()
    rows = rows[::-1]  # хронологически
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "adjusted_close": r[5], "volume": r[6]} for r in rows]


def _indicators(q):
    """Числовой тулкит по адж.ценам (вход технического/волнового агентов).

    indicators возвращают numpy-массивы (rsi/sma/atr/returns), скаляры (zscore/
    realized_vol/max_drawdown) и dict (bollinger/macd) — нормализуем к JSON-числам.
    """
    if len(q) < 30:
        return {"_note": "нет данных: < 30 баров для индикаторов"}
    import math
    px = [float(r["adjusted_close"]) for r in q if r["adjusted_close"] is not None]
    hi = [float(r["high"]) for r in q]
    lo = [float(r["low"]) for r in q]
    cl = [float(r["close"]) for r in q]
    vol = [float(r["volume"] or 0) for r in q]

    def lastval(arr):
        try:
            for x in reversed(list(arr)):
                if x is not None and not (isinstance(x, float) and math.isnan(x)):
                    return round(float(x), 4)
        except TypeError:
            return round(float(arr), 4)
        return None

    def scalar(x):
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 6)

    boll = ind.bollinger(px, 20, 2.0)
    macd = ind.macd(px)
    up_last, low_last = lastval(boll["upper"]), lastval(boll["lower"])
    out = {
        "asof": q[-1]["date"],
        "last_close": round(px[-1], 4),
        "rsi14": lastval(ind.rsi(px, 14)),
        "sma20": lastval(ind.sma(px, 20)),
        "sma50": lastval(ind.sma(px, 50)) if len(px) >= 50 else None,
        "realized_vol_annualized": scalar(ind.realized_vol(px[-21:]) * (252 ** 0.5)) if len(px) >= 22 else None,
        "atr14": lastval(ind.atr(hi, lo, cl, 14)),
        "ret_z_20": scalar(ind.zscore(ind.returns(px), 20)),
        "vol_z_20": scalar(ind.zscore(vol, 20)),
        "macd_hist": lastval(macd["hist"]),
        "bollinger_pos": (None if up_last is None else
                          "выше верхней" if px[-1] > up_last else
                          "ниже нижней" if px[-1] < low_last else "в полосе"),
        "max_drawdown_1y": scalar(ind.max_drawdown(px)),
    }
    return out


def _waves(q):
    """Числовая разметка волн Эллиотта по адж.ценам (вход агента-волновика, §4/§21).
    Код размечает пивоты и проверяет жёсткие правила; счёт интерпретирует LLM."""
    if len(q) < 10:
        return {"note": "нет данных: < 10 баров для разметки волн"}
    px = [float(r["adjusted_close"]) for r in q if r["adjusted_close"] is not None]
    return wv.wave_markup(px, threshold_pct=WAVE_THRESHOLD_PCT, recent_pivots=10)


def _news(con, limit=12):
    rows = con.execute(
        "SELECT published_at, source, title, lang FROM news "
        "WHERE dup_of IS NULL ORDER BY published_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"published_at": r[0], "source": r[1], "title": r[2], "lang": r[3]} for r in rows]


def build_context(theme="brent", asof=None):
    """Полный рыночный срез из реальных данных. asof=None → последние доступные данные."""
    universe = _load_yaml("config/universe.yaml")
    thresholds = _load_yaml("config/thresholds.yaml")
    causal = _load_yaml("knowledge/causal_links.yaml")
    precursors = _load_yaml("knowledge/precursors.yaml")

    ctx = {
        "asof": asof,
        "theme": theme,
        "universe": {
            "core_tradeable": CORE,
            "benchmark": universe.get("benchmark"),
            "themes": universe.get("themes"),
        },
        "calibration_status": {
            "thresholds_calibrated": thresholds.get("calibrated"),
            "timing": thresholds.get("timing", {}),
            "manipulation": thresholds.get("manipulation", {}),
            "fdr": {k: thresholds.get("fdr", {}).get(k) for k in ("procedure", "q_value_max")},
        },
        "knowledge": {
            "causal_links": (causal.get("links") or [])[:12],
            "causal_meta": {k: causal.get(k) for k in ("n_links", "empirical_lag_finding")},
            "precursors_meta": {k: precursors.get(k) for k in ("method", "n_bad_ticks_detected")},
        },
        "quotes": {},
        "indicators": {},
        "waves": {},
        "news": [],
        "data_gaps": [],  # честный реестр того, чего НЕТ в данных (П8)
    }

    if not DB.exists():
        ctx["data_gaps"].append("storage/oracle.db отсутствует — котировки и новости недоступны")
        return ctx

    con = sqlite3.connect(DB)
    try:
        for s in CORE:
            q = _quotes(con, s)
            if not q:
                ctx["data_gaps"].append(f"нет котировок по {s}")
                continue
            ctx["quotes"][s] = {"last": q[-1], "n_bars": len(q),
                                "first_date": q[0]["date"], "last_date": q[-1]["date"]}
            ctx["indicators"][s] = _indicators(q)
            ctx["waves"][s] = _waves(q)
        ctx["news"] = _news(con)
    finally:
        con.close()

    # честные пробелы данных §23.1 (то, чего нет в фиде)
    ctx["data_gaps"] += [
        "открытый интерес (OI) — нет в дневном фиде EODHD",
        "подразумеваемая волатильность опционов (IV) — не подключена",
        "глубина стакана / bid-ask — нет в дневных барах",
        "похожесть нарративов — требует длинной истории новостей (≈1 мес. доступно)",
        "позиционирование/потоки розницы/плечо — не подключены",
    ]
    return ctx


# ── §5.5: непересекающиеся проекции для разных агентов ───────────────────────────
def _common(ctx):
    """Минимальное ядро, видимое всем: универсум, статус калибровки, реестр пробелов."""
    return {
        "theme": ctx["theme"],
        "asof": ctx.get("asof"),
        "universe": ctx["universe"],
        "data_gaps": ctx["data_gaps"],
        "calibration_status": {"thresholds_calibrated": ctx["calibration_status"]["thresholds_calibrated"]},
    }


def slice_for(agent_id, ctx):
    """Проекция контекста под конкретного агента (§5.5). Возвращает dict-срез."""
    s = _common(ctx)
    quotes_brief = {k: v["last"] for k, v in ctx["quotes"].items()}

    if agent_id == "b_technical":
        s["indicators"] = ctx["indicators"]       # числовой тулкит индикаторов
        s["quotes"] = quotes_brief
    elif agent_id == "b_elliott_wave":
        s["waves"] = ctx["waves"]                  # числовая разметка волн — только волновику
        s["indicators"] = {k: {"last_close": v.get("last_close"), "atr14": v.get("atr14")}
                           for k, v in ctx["indicators"].items()}  # минимум для контекста хода
        s["quotes"] = quotes_brief
    elif agent_id == "b_behavioral_economist":
        s["news"] = ctx["news"]                    # медленные агрегаты + настроения
        s["positioning"] = "нет данных (позиционирование/плечо/потоки розницы не подключены)"
        s["quotes"] = quotes_brief
    elif agent_id == "b_fundamental":
        s["macro"] = "нет данных (макро/отчётность по ETF-прокси не подключены)"
        s["quotes"] = quotes_brief
    elif agent_id in ("b_causal_links", "c_cascades", "c_adjacent_domains"):
        s["causal_links"] = ctx["knowledge"]["causal_links"]
        s["causal_meta"] = ctx["knowledge"]["causal_meta"]
        s["news"] = ctx["news"][:6]
        s["quotes"] = quotes_brief
    elif agent_id == "b_historian_events":
        s["news"] = ctx["news"]
        s["analog_library"] = "используй только подтверждаемые срезом аналоги (П16)"
    elif agent_id == "b_historian_precursors":
        s["precursors_meta"] = ctx["knowledge"]["precursors_meta"]
        s["quotes"] = quotes_brief
        s["indicators"] = ctx["indicators"]
    elif agent_id == "b_cyclist":
        s["cycle_tests"] = "нет данных (статпроверка циклов §23 не подана в этот срез)"
        s["quotes"] = quotes_brief
    elif agent_id == "b_game_theory":
        s["news"] = ctx["news"]
        s["quotes"] = quotes_brief
    elif agent_id == "b_omens":
        s["news"] = ctx["news"][:6]
        s["quotes"] = quotes_brief
        s["quarantine"] = True
    elif agent_id == "d_timeliness":
        s["timing_thresholds"] = ctx["calibration_status"]["timing"]
        s["indicators"] = ctx["indicators"]
        s["news"] = ctx["news"][:6]
    elif agent_id == "d_anti_manipulation":
        s["manipulation_thresholds"] = ctx["calibration_status"]["manipulation"]
        s["news"] = ctx["news"]
        s["indicators"] = ctx["indicators"]
    elif agent_id in ("c_non_obviousness",):
        s["news"] = ctx["news"]
    elif agent_id in ("c_context_filter",):
        s["competence_ref"] = "USER_CONTEXT.md / §13 (сырьё, IT, ИИ, авто, космос, …)"
    elif agent_id in ("g_validator", "g_predictions_journalist", "g_outcome_analyst",
                      "g_weight_calibrator", "g_credibility"):
        s["news"] = ctx["news"][:4]
        s["quotes"] = quotes_brief
        s["note_control"] = "процедурный контроль: оцениваешь дисциплину, не рынок"
    else:
        s["quotes"] = quotes_brief
    return s


def render_user_prompt(agent_id, ctx):
    """Срез → текст user-промпта (компактный JSON + явная инструкция)."""
    s = slice_for(agent_id, ctx)
    blob = json.dumps(s, ensure_ascii=False, indent=1, default=str)
    return (
        "Поданный срез данных (market_slice). Используй ТОЛЬКО эти данные; чего здесь нет — "
        "«нет данных» (П8). Тикеры — только из universe.core_tradeable.\n\n"
        f"```json\n{blob}\n```\n\n"
        "Верни РОВНО один объект JSON по контракту из системного промпта."
    )
