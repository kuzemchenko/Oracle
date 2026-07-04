# -*- coding: utf-8 -*-
"""orchestrator/event_scan.py — ОТКРЫТЫЙ event-first скан (§6 Эт.1, Этап 1 PLAN_cascade_first.md).

Канон §6 Эт.1: «широкий скан — 200–500 сырых сигналов (события, тренды, аномалии частот)»,
FDR-контроль q<0.1 (Бенджамини–Хохберг). §17.2: события 1–4 порядка. Скан НЕ привязан к тикеру/теме:
сигналы тянутся из ТРЁХ открытых источников и сводятся в один пул, инструмент резолвится ПОЗЖЕ
(Этап 4), не здесь.

Источники сигналов:
  1. НОВОСТНЫЕ СОБЫТИЯ — кластеры заголовков (detect_news_clusters, открыто, не по тикерам).
     Частотный FDR требует длинной истории новостей (фон частот слов = null, П8 — см. thresholds.yaml);
     поэтому кластеры РАНЖИРУЮТСЯ по салиентности и помечаются честным ограничением, но НЕ
     отбрасываются — их вето даёт каскадный/состязательный контур ниже.
  2. ТРЕНДЫ — всплески поискового интереса (таблица trends): у КАЖДОГО ключа есть собственная
     история → эмпирический двусторонний p против своего фона → FDR-применим.
  3. ЦЕНА/ОБЪЁМ — z-аномалии по §9-разрешимому универсуму (U.sealable_universe — ДИНАМИЧЕСКИЙ,
     не список из 14) → normal-survival p → FDR-применим.

Статистические сигналы (тренды+цена) проходят ЕДИНЫЙ FDR (общая поправка на множественность).
Выход — ранжированный список КАНДИДАТ-СОБЫТИЙ + полный журнал отсева (прозрачность §6 Эт.6).
"""
import math
import sqlite3
import pathlib

from orchestrator import context as C
from orchestrator import universe_resolver as U
from mathlib import fdr
from mathlib import tailprob as TP
from mathlib.calibration import backgrounds as BG

ROOT = pathlib.Path(__file__).resolve().parents[1]

MIN_TREND_HISTORY = 8   # меньше — фон тренда не оценить честно (П8)

# F2#19 (§2.4): нормальный erfc(|z|/√2) занижал p на тяжелохвостых рядах → почти всё проходило FDR.
# Моделируем нуль Стьюдентом-t с тяжёлыми хвостами (mathlib/tailprob); объём предпочитаем на ЛОГ-шкале
# (сырой объём кратно скошен). df подобраны консервативно (тяжелее нормали). FDR q НЕ трогаем (B5).
_PRICE_METRICS = (
    ("ret_z_20", 5),        # доходности ~ t(5)
    ("vol_z_log_20", 6),    # лог-объём ~ t(6); фолбэк на сырой vol_z_20 → ещё тяжелее (t(3))
)
_VOL_RAW_FALLBACK_DF = 3


# ── Источник 2: тренды (всплеск интереса vs собственная история ключа) ──────────────
def trend_signals(trends_rows):
    """trends_rows: список (keyword, date, interest). Для каждого ключа — p последнего значения
    против его собственной истории (эмпирический двусторонний, непараметрический)."""
    by_kw = {}
    for kw, date, interest in trends_rows:
        if interest is None:
            continue
        by_kw.setdefault(kw, []).append((date, float(interest)))
    sigs = []
    for kw, series in by_kw.items():
        series.sort(key=lambda x: x[0])
        vals = [v for _, v in series]
        if len(vals) < MIN_TREND_HISTORY:
            continue
        latest, background = vals[-1], vals[:-1]
        try:
            p = BG.empirical_p_two_sided(latest, background)
        except ValueError:
            continue
        med = sorted(background)[len(background) // 2]
        sigs.append({"вид": "trend", "ключ": kw, "interest": round(latest, 1),
                     "медиана_фона": round(med, 1), "p_value": round(p, 4)})
    return sigs


# ── Источник 3: цена/объём по §9-универсуму ─────────────────────────────────────────
def price_vol_signals(indicators):
    """indicators: {symbol: {ret_z_20, vol_z_log_20|vol_z_20, ...}}. z → двусторонний p под ТЯЖЕЛО-
    ХВОСТЫМ нулём Стьюдента-t (F2#19), а не нормалью. Объём — лог-шкала, фолбэк на сырой с меньшим df."""
    sigs = []
    for sym, ind in (indicators or {}).items():
        for metric, df in _PRICE_METRICS:
            z = ind.get(metric)
            if metric == "vol_z_log_20" and not isinstance(z, (int, float)):
                metric, df, z = "vol_z_20", _VOL_RAW_FALLBACK_DF, ind.get("vol_z_20")
            if isinstance(z, (int, float)):
                p = TP.student_t_two_sided_p(z, df)
                sigs.append({"вид": "price", "символ": sym, "метрика": metric,
                             "z": round(z, 3), "df_нуля": df,
                             "p_value": round(max(min(p, 1.0), 0.0), 4)})
    return sigs


# ── Источник 1: новостные события (открытые кластеры) ───────────────────────────────
def news_event_signals(news):
    """Кластеры заголовков → кандидат-события (салиентность). Частотный FDR — П8 (нет длинной
    истории частот слов); ранжируем по салиентности, не отбрасываем (вето — ниже по контуру)."""
    from orchestrator import multi_event as ME   # ленивая (ME тянет funnel) — избегаем тяжёлого графа
    clusters = ME.detect_news_clusters(news or [], top=50)
    return [{"вид": "news", "ключи": cl["keywords"], "салиентность": cl["salience"],
             "пример": cl["sample"]} for cl in clusters]


# ── Сборка открытого скана + FDR ────────────────────────────────────────────────────
def scan_events(news=None, trends_rows=None, indicators=None, q_max=0.1):
    """Открытый event-first скан §6 Эт.1. Возвращает пул сырых сигналов, FDR по статистическим,
    ранжированные кандидат-события и честный реестр ограничений (П8)."""
    price = price_vol_signals(indicators or {})
    trends = trend_signals(trends_rows or [])
    statistical = price + trends                          # есть p → единый FDR
    pvals = [s["p_value"] for s in statistical]
    bh = fdr.benjamini_hochberg(pvals, q=q_max) if pvals else {"rejected": [], "qvalues": [], "n_signif": 0}
    for i, s in enumerate(statistical):
        s["q_value"] = round(bh["qvalues"][i], 4) if bh.get("qvalues") else None
        s["сигнал_после_FDR"] = bool(bh["rejected"][i]) if bh.get("rejected") else False
    news_events = news_event_signals(news)

    # Кандидат-события §6 Эт.1: значимые статистические + новостные кластеры (открыто, без тикер-якоря).
    events = []
    for s in statistical:
        if s["сигнал_после_FDR"]:
            label = s.get("ключ") or f'{s.get("символ")}:{s.get("метрика")}'
            events.append({"вид": s["вид"], "метка": label, "q_value": s["q_value"], "сырое": s})
    for ne in news_events:
        events.append({"вид": "news", "метка": " ".join(ne["ключи"][:3]),
                       "салиентность": ne["салиентность"], "сырое": ne})
    # порядок: новостная салиентность и статзначимость вперёд (грубый общий ранг)
    events.sort(key=lambda e: (e.get("салиентность") or 0, -(e.get("q_value") or 1.0)), reverse=True)

    return {
        "discovery_open": U.discovery_is_open(),
        "источники": {"price": len(price), "trends": len(trends), "news_clusters": len(news_events)},
        "сырых_сигналов": len(statistical) + len(news_events),
        "q_value_max": q_max, "процедура": "benjamini_hochberg (единый по цене+трендам)",
        "статистических_после_FDR": int(bh.get("n_signif", 0)),
        "сигналы": statistical,
        "новостные_события": news_events,
        "кандидат_события": events,
        "ограничение_П8": (
            "частотный FDR по новостям не применён: фон частот слов = null (история новостей < "
            "минимума, thresholds.yaml). Новостные кластеры ранжированы по салиентности и не "
            "отброшены — их вето даёт каскадный/состязательный контур (§6 Эт.3–5). "
            "Тренды и цена прошли единый FDR (есть собственные фоны). Кластеризуются только "
            "латиница/кириллица (en/ru) — CJK/арабские заголовки как шум для US-универсума выпадают."),
    }


def scan_events_live(q_max=0.1, news_limit=300, con=None):
    """Боевой открытый скан из БД: новости + тренды + цена по ДИНАМИЧЕСКОМУ §9-универсуму (брандмауэр)."""
    own = con is None
    if con is None:
        if not C.DB.exists():
            return {"error": "нет storage/oracle.db", "кандидат_события": [], "discovery_open": True}
        con = sqlite3.connect(str(C.DB), timeout=30)
    try:
        news = C._news(con, limit=news_limit)
        try:
            # П2а stage-review HIGH-3 (боковой канал): в скан идут ТОЛЬКО скан-ключи конфига
            # (темы news.yaml + extra) — ключи ПОЛЯ «внимание» (сиды/реестр) фетчатся для датчика,
            # но НЕ порождают кандидат-события и НЕ раздувают множественность единого FDR
            # (рост m менял бы порог Бенджамини-Хохберга для всех сигналов). Иначе П2а влиял бы
            # на отбор будущих прогонов — против инварианта «поле информационное» (§R4.2).
            from data import trends as TR
            scan_kws = set(TR.scan_keywords())
            trends_rows = [r for r in con.execute("SELECT keyword, date, interest FROM trends").fetchall()
                           if r[0] in scan_kws]
        except sqlite3.OperationalError:
            trends_rows = []
        indicators = {}
        for sym in U.sealable_universe(con=con):      # ← брандмауэр кормит скан, не хардкод CORE
            q = C._quotes(con, sym)
            if len(q) >= 30:
                indicators[sym] = C._indicators(q)
        return scan_events(news=news, trends_rows=trends_rows, indicators=indicators, q_max=q_max)
    finally:
        if own:
            con.close()
