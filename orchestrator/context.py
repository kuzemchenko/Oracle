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
from orchestrator import universe_resolver as U  # noqa: E402

# CORE = курируемая ЗАТРАВКА калибровки (единый источник правды — universe_resolver.CALIBRATION_SEED),
# а НЕ предел открытия. Скан событий/каскадов открыт (§6 Эт.1/§17.2 — U.discovery_is_open());
# что МОЖНО запечатать (§9/П16) — динамический U.sealable_universe(). Значение списка не изменилось.
CORE = U.CALIBRATION_SEED
WAVE_THRESHOLD_PCT = 0.05  # порог ZigZag для разметки волн (§4 «Волновик»); калибруется форвардом
MIN_THEME_HISTORY_BARS = 20  # §6/§23: меньше — нет волы/индикаторов/калибровки для §9-разрешимости


def _load_yaml(rel):
    p = ROOT / rel
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_theme(theme, con=None):
    """Сводит тему к тикеру. Возвращает (symbol|None, kind).

    kind: 'theme' (имя темы из universe.themes → proxy_etf) | 'core' (прямой тикер затравки) |
        'dynamic' (любой тикер с §9-источником цены/историей — брандмауэр §1 PLAN_cascade_first) | None.
    symbol=None → тикера нет источника цены/истории (нечего анализировать, П8).

    Ревизия 18.06 (поток идей): универсум АНАЛИЗА — динамический (sealable), не замороженный CORE.
    Калибровка — гейт только для ЗАПЕЧАТЫВАНИЯ, не для мышления: конкретную компанию из каскада
    (дальний чокпоинт-узел, не ETF) с историей котировок пускаем в research-разбор. Гард темы в
    run_funnel отклоняет лишь то, у чего нет цены/истории вовсе."""
    universe = _load_yaml("config/universe.yaml")
    themes = universe.get("themes") or {}
    t = (theme or "").strip()
    if t.lower() in themes:
        return (themes[t.lower()] or {}).get("proxy_etf"), "theme"
    if t.upper() in CORE:
        return t.upper(), "core"
    if U.is_sealable(t.upper(), con=con):       # динамический sealable-универсум (≥ MIN_SEALABLE_BARS)
        return t.upper(), "dynamic"
    return None, None


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


def _news(con, limit=12, keywords=None):
    """Свежие новости. Если keywords заданы — СНАЧАЛА совпавшие по заголовку (тема-якорь §17.2),
    затем добор свежими до limit. Так тематический прогон видит новости ПО ТЕМЕ, а не топ-дня."""
    rows = con.execute(
        "SELECT published_at, source, title, lang FROM news "
        "WHERE dup_of IS NULL ORDER BY published_at DESC LIMIT 400").fetchall()
    items = [{"published_at": r[0], "source": r[1], "title": r[2], "lang": r[3]} for r in rows]
    if keywords:
        kw = [k.lower() for k in keywords if k]
        hit = [it for it in items if any(k in (it["title"] or "").lower() for k in kw)]
        rest = [it for it in items if it not in hit]
        items = hit + rest
    return items[:limit]


def _fundamentals(con):
    """Скаляры фундаментала по символам (флоат, владение, short%float, оценка) из БД EODHD Tier 0.
    + borrow-прокси (§R4): cost-to-borrow EODHD не отдаёт → строим из short%float + Δшорта (Technicals
    из raw_json) + days-to-cover. Питает поведенческий хвост риск-агента и behavioral."""
    from mathlib import behavioral as BEH
    try:
        rows = con.execute(
            "SELECT symbol,name,sector,market_cap_mln,pe_ratio,shares_float,"
            "pct_insiders,pct_institutions,short_pct_float,raw_json FROM fundamentals").fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for r in rows:
        if r[5] is None and r[3] is None:
            continue
        try:
            tech = (json.loads(r[9]) or {}).get("Technicals") or {} if r[9] else {}
        except (json.JSONDecodeError, TypeError):
            tech = {}
        out[r[0]] = {"name": r[1], "sector": r[2], "market_cap_mln": r[3], "pe": r[4],
                     "free_float": r[5], "pct_insiders": r[6], "pct_institutions": r[7],
                     "short_pct_float": r[8],
                     "shares_short": tech.get("SharesShort"),
                     "shares_short_prior": tech.get("SharesShortPriorMonth"),
                     "short_ratio": tech.get("ShortRatio"),
                     "borrow_proxy": BEH.borrow_pressure(
                         short_pct_float=r[8], shares_short=tech.get("SharesShort"),
                         shares_short_prior=tech.get("SharesShortPriorMonth"),
                         short_ratio=tech.get("ShortRatio"))}
    return out


def _options(con):
    """Свёртки опционов по символам (ATM IV, skew, put/call OI/vol) из EODHD Unicorn Bay."""
    try:
        rows = con.execute("SELECT symbol, asof, summary FROM options_summary").fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for sym, asof, s in rows:
        try:
            d = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            continue
        if d and not d.get("insufficient"):
            out[sym] = {**d, "asof": asof}
    return out


def _insider_recent(con, symbol, limit=8):
    try:
        rows = con.execute(
            "SELECT tx_date,owner_name,owner_title,code,amount,price,acquired_disposed "
            "FROM insider_tx WHERE symbol=? ORDER BY tx_date DESC LIMIT ?", (symbol, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"date": r[0], "owner": r[1], "title": r[2], "code": r[3], "amount": r[4],
             "price": r[5], "acq_disp": r[6]} for r in rows]


def _earnings_next(con, symbol):
    try:
        row = con.execute(
            "SELECT report_date,before_after_market FROM earnings_calendar "
            "WHERE symbol=? AND report_date>=date('now') ORDER BY report_date LIMIT 1",
            (symbol,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return {"report_date": row[0], "when": row[1]} if row else None


def _theme_keywords(theme, universe):
    """Ключевые слова темы для фильтра новостей: имя темы + слова из event/related-имён."""
    themes = (universe or {}).get("themes") or {}
    t = (theme or "").strip().lower()
    kws = {t}
    meta = themes.get(t) or {}
    # имя/тикер прокси и связанных — без биржевого суффикса
    for sym in [meta.get("proxy_etf")] + list(meta.get("related") or []):
        if sym:
            kws.add(sym.split(".")[0].lower())
    # «spacex»→starlink/spacex; добавим явные синонимы из event-строки нет — задаём вручную для known
    extra = {"spacex": ["spacex", "starlink", "musk", "spcx"],
             "brent": ["brent", "oil", "opec", "crude"],
             "copper": ["copper", "freeport"],
             "ai_power": ["data center", "datacenter", "transformer", "electric grid",
                          "electricity demand", "grid", "power", "electrical steel",
                          "nuclear", "utility", "ai power"]}.get(t, [])
    kws.update(extra)
    return [k for k in kws if k and len(k) >= 3]


def build_context(theme="brent", asof=None, theme_focused=False):
    """Полный рыночный срез из реальных данных. asof=None → последние доступные данные.

    theme_focused=True (§17.2): новости приоритизируются по сущности темы (тема-якорь против
    дрейфа к топ-новости дня — урок SPCX/Иран), и в контекст кладётся блок theme_anchor."""
    universe = _load_yaml("config/universe.yaml")
    thresholds = _load_yaml("config/thresholds.yaml")
    causal = _load_yaml("knowledge/causal_links.yaml")
    precursors = _load_yaml("knowledge/precursors.yaml")
    themes = universe.get("themes") or {}
    theme_meta = themes.get((theme or "").strip().lower()) or {}

    ctx = {
        "asof": asof,
        "theme": theme,
        "theme_focused": theme_focused,
        "theme_meta": theme_meta,
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
        "fundamentals": {},   # EODHD Tier 0: флоат, владение, short%float, оценка
        "options": {},        # EODHD Unicorn Bay: ATM IV, skew, put/call OI/vol, ликвидность
        "insider_tx": {},     # инсайдерские сделки по активу темы и связанным
        "earnings_next": {},  # ближайшие отчёты (тайминг/cui bono)
        "theme_chain": None,  # тектоническая каскадная цепочка темы (карта + балл §5/П5)
        "theme_anchor": None, # §17.2: явная привязка прогона к теме (против дрейфа)
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
            # adj_closes — серия adjusted_close для ДЕТЕРМИНИРОВАННОЙ base_rate (F2#17, mathlib/base_rate).
            # Агентам НЕ уходит (в промпт идёт только quotes_brief={last}); используется кодом.
            adj_series = [float(r["adjusted_close"]) for r in q
                          if r.get("adjusted_close") is not None]
            ctx["quotes"][s] = {"last": q[-1], "n_bars": len(q),
                                "first_date": q[0]["date"], "last_date": q[-1]["date"],
                                "adj_closes": adj_series}
            ctx["indicators"][s] = _indicators(q)
            ctx["waves"][s] = _waves(q)
        # Новости: в тематическом фокусе — приоритет по ключевым словам темы (якорь §17.2).
        kws = _theme_keywords(theme, universe) if theme_focused else None
        ctx["news"] = _news(con, keywords=kws)
        # Фундаментал/инсайдеры/календарь (EODHD Tier 0) + опционы (Unicorn Bay аддон).
        ctx["fundamentals"] = _fundamentals(con)
        ctx["options"] = _options(con)
        theme_syms = [theme_meta.get("proxy_etf")] + list(theme_meta.get("related") or [])
        for s in [x for x in theme_syms if x] or CORE:
            ins = _insider_recent(con, s)
            if ins:
                ctx["insider_tx"][s] = ins
            en = _earnings_next(con, s)
            if en:
                ctx["earnings_next"][s] = en
    finally:
        con.close()

    # Тектоническая цепочка (пилот §5/П5): если у темы есть cascade_chain — карта + балл.
    chain_id = theme_meta.get("cascade_chain")
    if chain_id:
        try:
            from mathlib import tectonic as TEC
            chain = TEC.get_chain(chain_id)
            if chain:
                ctx["theme_chain"] = {"chain": chain, "tectonic": TEC.score_chain(chain)}
        except Exception:  # noqa: BLE001 — отсутствие карты не валит прогон (П8)
            ctx["theme_chain"] = None

    # Тема-якорь §17.2: явная директива против дрейфа к громкой новости дня.
    if theme_focused:
        sym = theme_meta.get("proxy_etf")
        related = theme_meta.get("related") or []
        structural = bool(theme_meta.get("structural"))
        ctx["theme_anchor"] = {
            "тема": theme,
            "актив_темы": sym,
            "событие": theme_meta.get("event"),
            "каскадные_звенья": related,
            "структурная": structural,
            "директива": (
                f"ЭТО ТЕМАТИЧЕСКИЙ ПРОГОН. Тема = {theme} ({sym}). Анализируй каскады 2–4 порядка "
                f"ИМЕННО ОТ этого события/актива. Громкая НЕсвязанная макро-новость дня (нефть, ставки, "
                f"геополитика) — НЕ повод бросать тему: упоминай её, только если она реально двигает "
                f"цепочку темы. " + (
                    f"АКТИВ ТЕМЫ {sym} НЕКАЛИБРУЕМ (мало истории) → по нему только КАРТА КАСКАДА и лист "
                    f"ожидания, БЕЗ запечатанного вероятностного прогноза (П16); торгуемые/вероятностные "
                    f"выводы выдвигай по КАЛИБРУЕМЫМ звеньям {related} (есть история и фундаментал)."
                    if structural else "")),
        }
        # Тектоническая подсказка: куда целиться (наименее отыгранный дальний чокпоинт-узел).
        tc = ctx.get("theme_chain")
        if tc and tc.get("tectonic"):
            far = tc["tectonic"].get("best_far_node") or {}
            ctx["theme_anchor"]["тектоника"] = {
                "потенциал": tc["tectonic"].get("tectonic_potential"),
                "окно_входа_дней": tc["tectonic"].get("lag_window_days"),
                "целевой_дальний_узел": far,
                "подсказка": (f"Движок оценил каскад: целься в НЕОТЫГРАННОЕ дальнее чокпоинт-звено "
                              f"«{far.get('node')}» ({far.get('instruments')}). 1-й порядок у истока "
                              f"обычно уже в цене (П5/П13) — ищи edge глубже по цепочке."),
            }

    # честные пробелы данных §23.1 (то, чего нет в фиде)
    n_opt = len(ctx.get("options") or {})
    ctx["data_gaps"] += [
        f"опционы (IV/OI/skew) — ПОДКЛЮЧЕНЫ (EODHD Unicorn Bay) по {n_opt} ликвидным символам; "
        "по новым/неликвидным (напр. SPCX свежий IPO) — нет данных (П8)",
        "глубина стакана / bid-ask интрадей — нет в дневных барах",
        "похожесть нарративов — требует длинной истории новостей (≈1 мес. доступно)",
        "потоки розницы / плечо — не подключены (short%float и IV-skew есть как прокси)",
    ]
    return ctx


# ── §5.5: непересекающиеся проекции для разных агентов ───────────────────────────
def _common(ctx):
    """Минимальное ядро, видимое всем: универсум, статус калибровки, реестр пробелов, ЯКОРЬ ТЕМЫ."""
    base = {
        "theme": ctx["theme"],
        "asof": ctx.get("asof"),
        "universe": ctx["universe"],
        "data_gaps": ctx["data_gaps"],
        "calibration_status": {"thresholds_calibrated": ctx["calibration_status"]["thresholds_calibrated"]},
    }
    if ctx.get("theme_anchor"):
        base["ЯКОРЬ_ТЕМЫ"] = ctx["theme_anchor"]   # против дрейфа к новости дня (§17.2)
    return base


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
        # Позиционирование теперь ЧАСТИЧНО есть (EODHD Tier 0): флоат, владение, short%float.
        s["fundamentals"] = ctx.get("fundamentals") or {}
        s["insider_tx"] = ctx.get("insider_tx") or {}
        s["options"] = ctx.get("options") or {}          # IV-skew = индикатор страха толпы
        s["positioning_note"] = ("short%float, владение — EODHD; IV/skew/put-call OI — опционы "
                                 "Unicorn Bay (где есть); потоки розницы — нет данных (П8)")
        s["quotes"] = quotes_brief
    elif agent_id == "b_fundamental":
        s["fundamentals"] = ctx.get("fundamentals") or {}   # флоат, mcap, P/E, владение (EODHD)
        s["earnings_next"] = ctx.get("earnings_next") or {}
        s["quotes"] = quotes_brief
    elif agent_id in ("b_causal_links", "c_cascades", "c_adjacent_domains"):
        s["causal_links"] = ctx["knowledge"]["causal_links"]
        s["causal_meta"] = ctx["knowledge"]["causal_meta"]
        s["news"] = ctx["news"][:6]
        s["fundamentals"] = ctx.get("fundamentals") or {}   # флоат/владение звеньев каскада
        if ctx.get("theme_chain"):                          # карта тектонической цепочки (пилот §5)
            s["каскадная_цепочка_темы"] = ctx["theme_chain"]
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
        s["options"] = ctx.get("options") or {}              # всплеск IV = «дорогие ожидания» (П13)
    elif agent_id == "d_anti_manipulation":
        s["manipulation_thresholds"] = ctx["calibration_status"]["manipulation"]
        s["news"] = ctx["news"]
        s["indicators"] = ctx["indicators"]
        s["insider_tx"] = ctx.get("insider_tx") or {}        # «кто продаёт нам» (§4 детектор 2)
        s["earnings_next"] = ctx.get("earnings_next") or {}  # cui bono: совпадение с отчётами (§14)
        s["fundamentals"] = ctx.get("fundamentals") or {}
        s["options"] = ctx.get("options") or {}              # открытый интерес/skew (§4 детектор 2/3)
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
