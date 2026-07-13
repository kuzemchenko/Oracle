# -*- coding: utf-8 -*-
"""orchestrator/segment_screen.py — Э4(б) «Перебор мира»: ДЕТЕРМИНИРОВАННЫЙ скрин
сегмент карты → полный список ликвидных торгуемых инструментов.

Программа «Поисковый движок» (spec/ROADMAP_2026-07_search_engine.md, этап Э4). Код, не LLM
(Инв#6): карта мира даёт сегмент (сектор/индустрии EODHD), скрин перечисляет инструменты.

Источник — EODHD Screener API. Доступность в подписке ПРОВЕРЕНА ЖИВЬЁМ 13.07.2026 (этот worktree,
одиночные запросы): GET /api/screener с filters=[["sector","=",...],["exchange","=","us"],
["avgvol_200d",">",...]] и sort=market_capitalization.desc → HTTP 200, поля code/sector/industry/
market_capitalization/avgvol_200d. Фолбэк (если screener недоступен/упал не-квотно) — Tier0-фундаментал
из storage/oracle.db (таблица fundamentals, сектор/индустрия; покрытие честно малое — только
уже профетченные символы) + ликвидность из quotes.

Фильтр ликвидности — config/universe.yaml liquidity_filter.min_avg_daily_volume (§14).
Sealable-гейт — orchestrator/universe_resolver.is_sealable (§9/П16).

Добор истории новых тикеров — БЕЗ дневного потолка (решение владельца 13.07 №5); при квотных
ошибках EODHD (402/429/payment/quota) — алерт владельцу через существующий канал
journal/notices.jsonl (формат ops/auto_review._notice; ТОЛЬКО append — бот читает по курсору
номера строки, ротация/перезапись ломает доставку).
"""
import datetime
import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml                                            # noqa: E402

from orchestrator import universe_resolver as U        # noqa: E402

DB = ROOT / "storage" / "oracle.db"
UNIVERSE = ROOT / "config" / "universe.yaml"
NOTICES = ROOT / "journal" / "notices.jsonl"
SCREENER_URL = "https://eodhd.com/api/screener"

# Сектора EODHD (наблюдённый словарь screener/fundamentals; General.Sector). Единая точка правды
# для валидации карты мира (world_map) и построения фильтров скрина.
EODHD_SECTORS = [
    "Basic Materials", "Communication Services", "Consumer Cyclical", "Consumer Defensive",
    "Energy", "Financial Services", "Healthcare", "Industrials", "Real Estate",
    "Technology", "Utilities",
]

PAGE_LIMIT = 100          # максимум записей на страницу screener (лимит API)
MAX_PAGES_PER_FILTER = 5  # кэп пагинации одного фильтра (гигиена: 500 инструментов на фильтр за глаза)


class QuotaError(RuntimeError):
    """Квотная ошибка EODHD (402/429/payment/quota) — исчерпание НЕ должно быть тихим
    (решение владельца 13.07 №5): вызывающий обязан дать алерт в notices-канал."""


def _quota_marker(text):
    t = str(text).lower()
    return any(m in t for m in ("http 402", "http 429", "payment", "quota", "too many requests"))


def notify_owner(text, notices_path=None):
    """Заметка владельцу: append-строка в journal/notices.jsonl — тот же канал и формат, что
    ops/auto_review._notice (бот пушит новые записи по курсору-номеру строки; ТОЛЬКО append)."""
    path = pathlib.Path(notices_path) if notices_path else NOTICES
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "text": text}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def min_avg_daily_volume(universe=None):
    """Порог ликвидности из config/universe.yaml (§14). Нет ключа → консервативные 100000."""
    if universe is None:
        with open(UNIVERSE, encoding="utf-8") as f:
            universe = yaml.safe_load(f) or {}
    return int(((universe.get("liquidity_filter") or {}).get("min_avg_daily_volume", 100000)))


# ── EODHD Screener ────────────────────────────────────────────────────────────────
def _http_fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "oracle/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code in (402, 429):
            raise QuotaError(f"HTTP {e.code} screener EODHD — квота/оплата") from e
        raise RuntimeError(f"HTTP {e.code} screener EODHD") from e


def fetch_screener_page(api_key, filters, *, offset=0, limit=PAGE_LIMIT,
                        sort="market_capitalization.desc", fetch=None):
    """Одна страница screener. filters — список триплетов EODHD [["sector","=","Industrials"],...].
    fetch(url)->dict инъектируется в тестах (сеть в тестах запрещена — фикстуры)."""
    q = urllib.parse.urlencode({
        "api_token": api_key, "filters": json.dumps(filters),
        "sort": sort, "limit": int(limit), "offset": int(offset)})
    data = (fetch or _http_fetch)(f"{SCREENER_URL}?{q}")
    # Э4-ревью (HIGH): EODHD может вернуть квотную/платёжную ошибку в ТЕЛЕ HTTP-200 JSON
    # ({"error":"quota exceeded"} / {"message":"payment required"} / "limit"), а не HTTP-кодом.
    # Без детекта тела это маскировалось под пустой скрин, и алерт владельцу (решение №5) не шёл.
    if isinstance(data, dict):
        msg = str(data.get("error") or data.get("message") or "").lower()
        if msg and any(m in msg for m in ("quota", "payment", "limit", "too many", "402", "429")):
            raise QuotaError(f"квота/оплата в теле ответа screener EODHD: {msg[:80]}")
    return (data or {}).get("data") or []


def screener_available(api_key, fetch=None):
    """Дешёвая проверка доступности screener в подписке (1 запрос, limit=1).
    Возвращает (True|False, detail). Квотная ошибка = доступен, но квота (True с пометкой)."""
    try:
        rows = fetch_screener_page(api_key, [["exchange", "=", "us"]], limit=1, fetch=fetch)
        return bool(rows) or rows == [], "ok"
    except QuotaError as e:
        return True, f"доступен, но квота: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _seg_filter_levels(segment, min_vol):
    """Уровни фильтров screener для сегмента В ПОРЯДКЕ ПРИОРИТЕТА (Э4-ревью medium):
    сначала по индустриям (уже), при пустом результате — деградация на секторы (шире).
    Возвращает [(уровень, [наборы_фильтров])]. Ликвидность зашита в запрос (avgvol_200d)."""
    base = [["exchange", "=", "us"], ["avgvol_200d", ">=", int(min_vol)]]
    levels = []
    ind = [[["industry", "=", i]] + base for i in (segment.get("индустрии") or [])]
    if ind:
        levels.append(("industry", ind))
    sec = [[["sector", "=", s]] + base for s in (segment.get("секторы") or [])]
    if sec:
        levels.append(("sector", sec))
    return levels


def _screen_api_level(api_key, filter_sets, *, min_vol, max_instruments, fetch=None):
    """Один уровень скрина (набор фильтров) → строки инструментов."""
    seen, rows = set(), []
    for filters in filter_sets:
        for page in range(MAX_PAGES_PER_FILTER):
            if len(rows) >= max_instruments:
                break
            batch = fetch_screener_page(api_key, filters, offset=page * PAGE_LIMIT,
                                        limit=PAGE_LIMIT, fetch=fetch)
            if not batch:
                break
            for r in batch:
                code = (r.get("code") or "").strip().upper()
                if not code or code in seen:
                    continue
                seen.add(code)
                avgvol = r.get("avgvol_200d")
                if avgvol is None or float(avgvol) < min_vol:   # ремень к подтяжкам фильтра API
                    continue
                rows.append({"symbol": f"{code}.US",
                             "sector": r.get("sector"), "industry": r.get("industry"),
                             "market_cap": r.get("market_capitalization"),
                             "avg_volume": float(avgvol), "источник_скрина": "eodhd_screener"})
                if len(rows) >= max_instruments:
                    break
            if len(batch) < PAGE_LIMIT:
                break
    rows.sort(key=lambda r: (-(r["market_cap"] or 0), r["symbol"]))
    return rows


def screen_segment_api(segment, api_key, *, min_vol, max_instruments, fetch=None):
    """Скрин сегмента через EODHD screener с ДЕГРАДАЦИЕЙ уровня: индустрии → (если пусто) секторы.
    Э4-ревью (medium): невалидная/опечатанная «индустрия» карты давала пустой скрин и не откатывалась
    на сектор. Символы нормализуются к SYMBOL.US, сорт по капитализации (детерминирован)."""
    for уровень, filter_sets in _seg_filter_levels(segment, min_vol):
        rows = _screen_api_level(api_key, filter_sets, min_vol=min_vol,
                                 max_instruments=max_instruments, fetch=fetch)
        if rows:
            for r in rows:
                r["уровень_скрина"] = уровень
            return rows
    return []


def screen_segment_db(segment, con, *, min_vol, max_instruments):
    """Фолбэк Tier0: fundamentals (сектор/индустрия) из storage/oracle.db + средний объём из quotes.
    Покрытие честно ограничено уже профетченными символами (П8: это НЕ полный рынок — помечаем)."""
    secs = segment.get("секторы") or []
    inds = segment.get("индустрии") or []
    if not secs and not inds:
        return []
    # Э4-ревью (medium): деградация уровня и в БД-фолбэке — индустрии, при пустом результате секторы.
    levels = []
    if inds:
        levels.append(("industry", "industry IN (%s)" % ",".join("?" * len(inds)), list(inds)))
    if secs:
        levels.append(("sector", "sector IN (%s)" % ",".join("?" * len(secs)), list(secs)))
    frows, уровень = [], None
    for lvl, cond, params in levels:
        try:
            frows = con.execute(
                f"SELECT symbol, sector, industry, market_cap_mln FROM fundamentals WHERE {cond}",
                params).fetchall()
        except sqlite3.OperationalError:
            return []
        if frows:
            уровень = lvl
            break
    out = []
    for sym, sector, industry, mcap in frows:
        # средний объём последних 40 баров (устойчивее полного среднего за годы)
        vrow = con.execute(
            "SELECT AVG(v) FROM (SELECT volume AS v FROM quotes WHERE symbol=? "
            "AND volume IS NOT NULL ORDER BY date DESC LIMIT 40)", (sym,)).fetchone()
        avgvol = float(vrow[0]) if vrow and vrow[0] is not None else None
        if avgvol is None or avgvol < min_vol:
            continue
        out.append({"symbol": sym, "sector": sector, "industry": industry,
                    "market_cap": (mcap * 1e6) if mcap else None,
                    "avg_volume": avgvol, "уровень_скрина": уровень,
                    "источник_скрина": "fundamentals_db (фолбэк, покрытие частичное)"})
    out.sort(key=lambda r: (-(r["market_cap"] or 0), r["symbol"]))
    return out[:max_instruments]


def screen_segment(segment, *, api_key=None, con=None, db=None, universe=None,
                   max_instruments=300, fetch=None, notices_path=None):
    """Полный скрин одного сегмента карты: screener → фолбэк БД. Возвращает
    {"инструменты": [...], "источник": ..., "отказ": None|причина}.

    Квотная ошибка EODHD → алерт владельцу (notices, решение №5) + честный переход на фолбэк БД."""
    min_vol = min_avg_daily_volume(universe)
    отказ = None
    def _src(base, rows):
        """Честная пометка деградации уровня (индустрии → секторы), если она случилась (medium)."""
        used = rows[0].get("уровень_скрина") if rows else None
        if used == "sector" and (segment.get("индустрии")):
            return base + " (деградация: индустрии карты пусты → скрин по сектору)"
        return base
    if api_key:
        try:
            rows = screen_segment_api(segment, api_key, min_vol=min_vol,
                                      max_instruments=max_instruments, fetch=fetch)
            return {"инструменты": rows, "источник": _src("eodhd_screener", rows), "отказ": None}
        except QuotaError as e:
            notify_owner(
                f"⚠ Э4-скрин: квота EODHD исчерпана на screener ({e}) — сегмент "
                f"«{segment.get('сегмент')}» скринится фолбэком из локальной БД (покрытие частичное). "
                f"Решение владельца №5: добор без потолка, исчерпание не молчит.", notices_path)
            отказ = f"квота EODHD: {e}"
        except Exception as e:  # noqa: BLE001 — не-квотный сбой API → фолбэк, причина в протокол
            отказ = f"screener недоступен: {type(e).__name__}: {e}"
    own = con is None
    if con is None:
        con = sqlite3.connect(str(db or DB), timeout=30)
    try:
        rows = screen_segment_db(segment, con, min_vol=min_vol, max_instruments=max_instruments)
    finally:
        if own:
            con.close()
    return {"инструменты": rows,
            "источник": _src("fundamentals_db", rows) + (f" (после: {отказ})" if отказ else ""),
            "отказ": None if rows else (отказ or "нет данных скрина: ни screener, ни фундаментал БД")}


def annotate_sealable(instruments, con=None, db=None):
    """§9-гейт: пометить каждому инструменту sealable (есть источник цены с историей ≥ порога).
    НЕ отбрасывает — классификацию отказов делает конвейер (Э4(д))."""
    own = con is None
    if con is None:
        con = sqlite3.connect(str(db or DB), timeout=30)
    try:
        for r in instruments:
            r["sealable"] = U.is_sealable(r["symbol"], con=con)
    finally:
        if own:
            con.close()
    return instruments


def backfill_history(symbols, api_key, *, con, min_bars=U.MIN_SEALABLE_BARS,
                     notices_path=None, history_from="2019-01-01"):
    """Добор истории новых тикеров через data.eodhd.ensure_history — БЕЗ дневного потолка
    (решение владельца 13.07 №5). Квотные ошибки в failed → алерт (не тихо). Требует
    ПИШУЩЕЕ соединение с БД — в разработке боевая БД read-only, добор только с Э5/по санкции."""
    from data import eodhd as E
    res = E.ensure_history(con, symbols, api_key, min_bars=min_bars, history_from=history_from)
    quota_fails = [f for f in res.get("failed", []) if _quota_marker(f.get("почему"))]
    if quota_fails:
        syms = ", ".join(f["symbol"] for f in quota_fails[:8])
        notify_owner(
            f"⚠ Э4-добор истории: квота EODHD исчерпана ({len(quota_fails)} тикеров, напр. {syms}). "
            f"Добор без потолка (решение №5), но исчерпание квоты не молчит — дозакачка при "
            f"следующем прогоне/пере-скрине.", notices_path)
    return res
