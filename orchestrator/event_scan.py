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
import datetime

from orchestrator import context as C
from orchestrator import universe_resolver as U
from mathlib import fdr
from mathlib import tailprob as TP
from mathlib.calibration import backgrounds as BG

ROOT = pathlib.Path(__file__).resolve().parents[1]

MIN_TREND_HISTORY = 8   # меньше — фон тренда не оценить честно (П8)
MAX_BAR_AGE_DAYS = 7    # Д1 #8: последний бар инструмента старше — котировки протухли, сигнал не строим

# F2#19 (§2.4): нормальный erfc(|z|/√2) занижал p на тяжелохвостых рядах → почти всё проходило FDR.
# Моделируем нуль Стьюдентом-t с тяжёлыми хвостами (mathlib/tailprob); объём предпочитаем на ЛОГ-шкале
# (сырой объём кратно скошен). FDR q НЕ трогаем (B5).
# Д1 (ROADMAP 2026-07): df-константы ниже были назначены «консервативно», эмпирически не подбирались
# и передушили скан (0 после FDR 12 дней подряд). Теперь df читается per-instrument из
# config/thresholds.yaml (fdr.tail_df — walk-forward, ops/calibrate_fdr_background.py);
# константы остаются ЧЕСТНЫМ фолбэком при отсутствии секции/ключа (fail-safe, поведение
# без секции — байт-в-байт прежнее, тест test_event_scan_tail_df).
_PRICE_METRICS = (
    ("ret_z_20", 5),        # доходности ~ t(5) — фолбэк-константа F2#19
    ("vol_z_log_20", 6),    # лог-объём ~ t(6); фолбэк на сырой vol_z_20 → ещё тяжелее (t(3))
)
_VOL_RAW_FALLBACK_DF = 3

# Д1-Вариант 2 (решение владельца 14.07): FDR (Бенджамини-Хохберг) структурно душил ВЕСЬ передний
# скан — на единый пул ~470 проверок планка одиночки q/m ≈ 0.0002, а непараметрический «пол» p тренда
# = 1/(история+1) ≈ 0.009 (медиана истории 116 дней) её физически не берёт → трендовый канал не мог
# выстрелить ПО ПОСТРОЕНИЮ, а ценовой пропускал лишь редкие 4-5σ-шоки (honest replay 13.07: 0 за 22 дня).
# РЕШЕНИЕ: FDR остаётся ЧЕСТНЫМ ЯРЛЫКОМ (сигнал_после_FDR, q_value → табло §15), а порождение идей
# переключено на «заметные аномалии» — как новости: ранг по значимости + фиксированный кап ширины.
# НАСТОЯЩИЙ гейт качества — слепой суд (планка 3.0), §11/KILL/лимиты/журналы НЕ тронуты. Кап — это
# бюджет ширины (аналог detect_news_clusters(top=50)), а НЕ порог гейта денег: перебор порогов
# (§R-рамка программы) не нарушен, порог зафиксирован ДО прогона с обоснованием.
CAND_PRICE_TOP = 15   # топ ценовых кандидатов к суду (по возрастанию p)
CAND_TREND_TOP = 8    # топ трендовых кандидатов; 15+8+новостные кластеры ≈ 30-50 воронки §6
# Порог «заметности» кандидата: номинальный (нескорректированный) двусторонний p < 0.05 — классический
# уровень значимости одиночной проверки (|z|≳2.5). Делает отбор СОБЫТИЙНО-ЧУВСТВИТЕЛЬНЫМ: тихий день →
# мало кандидатов, событийный → до капа; без него порог 0.5 давал бы «топ-15 шевелящихся каждый день»
# (шум в суд даже в штиль). Это НЕ порог денежного гейта (тот — планка суда 3.0, не тронута) и делает
# отбор СТРОЖЕ, а не «пропускает до пролезания» → §R-рамка (перебор порогов) не нарушена. Зафиксирован
# ДО прогона по honest-replay 14.07 (0.05 против 0.5/0.01 — 0.05 = событийная чувствительность без
# голодания). FDR (Бенджамини-Хохберг) остаётся ЯРЛЫКОМ честности §15, здесь не участвует.
CAND_P_MAX = 0.05


def tail_df_from_thresholds(thresholds=None):
    """Секция fdr.tail_df из config/thresholds.yaml (dict | None = прежнее поведение констант).
    thresholds не передан → файл читается здесь; любой сбой чтения → None (fail-safe)."""
    try:
        if thresholds is None:
            thresholds = C._load_yaml("config/thresholds.yaml")
        td = ((thresholds or {}).get("fdr") or {}).get("tail_df")
        return td if isinstance(td, dict) else None
    except Exception:  # noqa: BLE001 — битый конфиг не валит скан, скан честно падает на константы
        return None


def _resolve_df(tail_df, symbol, metric, const_df):
    """df для пары (инструмент, метрика): per-instrument → фолбэк калибровки → константа F2#19.
    Возвращает (df, источник) — источник уходит в протокол скана (П8: видно, чем посчитан p)."""
    per = ((tail_df or {}).get("per_instrument") or {}).get(symbol) or {}
    v = per.get(metric)
    # bool — подкласс int (True==1): df=1.0 из ошибочного True в конфиге отравил бы t-хвост.
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v), "per_instrument"
    fb = ((tail_df or {}).get("fallback") or {}).get(metric)
    if isinstance(fb, (int, float)) and not isinstance(fb, bool) and fb > 0:
        return float(fb), "фолбэк_калибровки"
    return const_df, "константа_F2#19"


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
        # _p_raw — ПОЛНАЯ точность для FDR; p_value (округлённый) только для протокола (Д1 #5:
        # BH обязан работать на неокруглённых p, иначе округление до 4 знаков меняет открытия).
        sigs.append({"вид": "trend", "ключ": kw, "interest": round(latest, 1),
                     "медиана_фона": round(med, 1), "p_value": round(p, 4),
                     "_p_raw": float(min(max(p, 0.0), 1.0))})
    return sigs


# ── Источник 3: цена/объём по §9-универсуму ─────────────────────────────────────────
def price_vol_signals(indicators, tail_df=None):
    """indicators: {symbol: {ret_z_20, vol_z_log_20|vol_z_20, ...}}. z → двусторонний p под ТЯЖЕЛО-
    ХВОСТЫМ нулём Стьюдента-t (F2#19), а не нормалью. Объём — лог-шкала, фолбэк на сырой с меньшим df.

    tail_df (Д1): секция fdr.tail_df из thresholds.yaml — df per-instrument с провенансом;
    None → прежние константы и БАЙТ-В-БАЙТ прежний состав полей сигнала (без df_источник)."""
    sigs = []
    for sym, ind in (indicators or {}).items():
        for metric, df in _PRICE_METRICS:
            z = ind.get(metric)
            if metric == "vol_z_log_20" and not isinstance(z, (int, float)):
                metric, df, z = "vol_z_20", _VOL_RAW_FALLBACK_DF, ind.get("vol_z_20")
            if isinstance(z, (int, float)):
                src = None
                if tail_df is not None:
                    df, src = _resolve_df(tail_df, sym, metric, df)
                p = TP.student_t_two_sided_p(z, df)
                p_clamped = max(min(p, 1.0), 0.0)
                sig = {"вид": "price", "символ": sym, "метрика": metric,
                       "z": round(z, 3), "df_нуля": df,
                       "p_value": round(p_clamped, 4),
                       "_p_raw": float(p_clamped)}   # Д1 #5: полная точность для BH (см. scan_events)
                if src is not None:
                    sig["df_источник"] = src
                sigs.append(sig)
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
def _bar_age_days(asof_str, ref_date):
    """Возраст последнего бара в календарных днях (ref_date − дата бара). None при неразборе."""
    try:
        return (ref_date - datetime.date.fromisoformat(str(asof_str)[:10])).days
    except (ValueError, TypeError):
        return None


def _mark_candidates(statistical):
    """Д1-Вариант2 (решение владельца 14.07): помечает s["кандидат"]=True у топ-N сигналов КАЖДОГО
    канала по ВОЗРАСТАНИЮ сырого p (_p_raw) — наибольшая аномальность вперёд. Фиксированный кап
    ширины (CAND_PRICE_TOP / CAND_TREND_TOP) — контроль стоимости/шума перенесён на слепой суд
    (планка 3.0), а не на передний FDR-турникет. FDR-ярлык (сигнал_после_FDR) НЕ трогается —
    табло честности §15 считает по-прежнему. Ключевой эффект: трендовый канал, который по BH не
    мог выстрелить в принципе (пол p ≈ 0.009 > планка q/m ≈ 0.0002), снова доходит до суда."""
    for vид, top in (("price", CAND_PRICE_TOP), ("trend", CAND_TREND_TOP)):
        chan = sorted((s for s in statistical if s.get("вид") == vид
                       and s.get("_p_raw", 1.0) < CAND_P_MAX),            # порог заметности
                      key=lambda s: s.get("_p_raw", 1.0))
        for s in chan[:top]:
            s["кандидат"] = True
    for s in statistical:
        s.setdefault("кандидат", False)


def scan_events(news=None, trends_rows=None, indicators=None, q_max=0.1, tail_df=None,
                asof_date=None, max_bar_age_days=MAX_BAR_AGE_DAYS):
    """Открытый event-first скан §6 Эт.1. Возвращает пул сырых сигналов, FDR по статистическим,
    ранжированные кандидат-события и честный реестр ограничений (П8).

    tail_df (Д1) — df t-нуля per-instrument (fdr.tail_df из thresholds.yaml);
    None → прежние df-константы, протокол байт-в-байт прежний.

    asof_date (Д1 #8) — дата прогона (date|ISO). Если задана, инструменты с ПРОТУХШИМ последним
    баром (возраст > max_bar_age_days) исключаются из ценового скана — сигнал не строится на
    несвежих котировках (delisted/пропал фид). None → прежнее поведение (гейт давности выключен)."""
    # Д1 #8: гейт давности бара (боевой). Отфильтрованные инструменты — в честный реестр (П8).
    stale = []
    if asof_date is not None and indicators:
        ref = asof_date if isinstance(asof_date, datetime.date) else \
            datetime.date.fromisoformat(str(asof_date)[:10])
        kept = {}
        for sym, ind in indicators.items():
            age = _bar_age_days((ind or {}).get("asof"), ref) if isinstance(ind, dict) else None
            if age is not None and age > max_bar_age_days:
                stale.append({"символ": sym, "последний_бар": ind.get("asof"), "давность_дней": age})
            else:
                kept[sym] = ind
        indicators = kept
    price = price_vol_signals(indicators or {}, tail_df=tail_df)
    trends = trend_signals(trends_rows or [])
    statistical = price + trends                          # есть p → единый FDR
    # Д1 #5 (кросс-ревью): BH считается по ПОЛНОЙ точности p (_p_raw), НЕ по округлённому до 4
    # знаков p_value — иначе на больших m истинный p=0.000049 → 0.0000 гарантированно проходит,
    # 0.05004 → 0.05 ложно проходит, меняя набор открытий. Округление — только протокол/показ.
    pvals = [s["_p_raw"] for s in statistical]
    bh = fdr.benjamini_hochberg(pvals, q=q_max) if pvals else {"rejected": [], "qvalues": [], "n_signif": 0}
    for i, s in enumerate(statistical):
        s["q_value"] = round(bh["qvalues"][i], 4) if bh.get("qvalues") else None
        s["сигнал_после_FDR"] = bool(bh["rejected"][i]) if bh.get("rejected") else False
    # Д1-Вариант2: кандидат к суду = топ по значимости в КАЖДОМ канале (фикс. кап ширины, НЕ гейт
    # денег). Метим ДО удаления _p_raw (полная точность p — основа ранга).
    _mark_candidates(statistical)
    for s in statistical:
        s.pop("_p_raw", None)                             # внутреннее поле не выходит наружу
    news_events = news_event_signals(news)

    # Кандидат-события §6 Эт.1: заметные статистические аномалии + новостные кластеры (открыто, без
    # тикер-якоря). Д1-Вариант2: пускаем «кандидатов» (ранг+кап), а НЕ прошедших строгий BH — контроль
    # шума перенесён на слепой суд. FDR-ярлык (сигнал_после_FDR) едет вместе для табло честности.
    events = []
    for s in statistical:
        if s.get("кандидат"):
            label = s.get("ключ") or f'{s.get("символ")}:{s.get("метрика")}'
            events.append({"вид": s["вид"], "метка": label, "q_value": s["q_value"],
                           "сигнал_после_FDR": s["сигнал_после_FDR"], "сырое": s})
    for ne in news_events:
        events.append({"вид": "news", "метка": " ".join(ne["ключи"][:3]),
                       "салиентность": ne["салиентность"], "сырое": ne})
    # порядок: новостная салиентность и статзначимость вперёд (грубый общий ранг)
    events.sort(key=lambda e: (e.get("салиентность") or 0, -(e.get("q_value") or 1.0)), reverse=True)

    out_tail = None
    if tail_df is not None:
        by_src = {}
        for s in price:
            by_src[s.get("df_источник")] = by_src.get(s.get("df_источник"), 0) + 1
        out_tail = {"источник": "config/thresholds.yaml: fdr.tail_df (Д1, walk-forward "
                                "ops/calibrate_fdr_background.py)",
                    "df_по_источникам": by_src,
                    "фолбэк": {k: v for k, v in ((tail_df.get("fallback") or {}).items())
                               if k != "note"}}
    return {
        "discovery_open": U.discovery_is_open(),
        "источники": {"price": len(price), "trends": len(trends), "news_clusters": len(news_events)},
        "сырых_сигналов": len(statistical) + len(news_events),
        "q_value_max": q_max,
        "процедура": "benjamini_hochberg (ЯРЛЫК честности §15) + кандидатский ранг к суду (Д1-Вариант2)",
        **({"tail_df_протокол": out_tail} if out_tail is not None else {}),
        **({"протухшие_бары": stale} if stale else {}),
        "статистических_после_FDR": int(bh.get("n_signif", 0)),
        "кандидатов_к_суду": sum(1 for s in statistical if s.get("кандидат")),
        "сигналы": statistical,
        "новостные_события": news_events,
        "кандидат_события": events,
        "ограничение_П8": (
            "частотный FDR по новостям не применён: фон частот слов = null (история новостей < "
            "минимума, thresholds.yaml). Новостные кластеры ранжированы по салиентности и не "
            "отброшены — их вето даёт каскадный/состязательный контур (§6 Эт.3–5). "
            "Д1-Вариант2 (решение владельца 14.07): единый FDR по цене+трендам ОСТАЁТСЯ как ярлык "
            "честности (сигнал_после_FDR, q_value → табло §15), но НЕ гейтит порождение идей — к суду "
            "(планка 3.0, настоящий гейт) идут топ-кандидаты по значимости в каждом канале (кап "
            "ширины CAND_PRICE_TOP/CAND_TREND_TOP, аналог detect_news_clusters(top=50)). Причина: на "
            "едином пуле ~470 проверок BH-планка одиночки q/m≈0.0002 < пол p тренда 1/(n+1)≈0.009 → "
            "трендовый канал не мог выстрелить по построению, ценовой — лишь на редких 4-5σ. "
            "Кластеризуются только латиница/кириллица (en/ru) — CJK/арабские заголовки как шум для "
            "US-универсума выпадают."),
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
        return scan_events(news=news, trends_rows=trends_rows, indicators=indicators, q_max=q_max,
                           tail_df=tail_df_from_thresholds(),    # Д1: df per-instrument (fail-safe)
                           asof_date=datetime.date.today())      # Д1 #8: гейт давности бара
    finally:
        if own:
            con.close()
