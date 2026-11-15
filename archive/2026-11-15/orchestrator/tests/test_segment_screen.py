# -*- coding: utf-8 -*-
"""Тесты Э4(б) — детерминированный скрин сегмент→инструменты (orchestrator/segment_screen.py).
Сеть в тестах ЗАПРЕЩЕНА: screener — через инъекцию fetch (фикстуры), БД — in-memory."""
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import segment_screen as SS      # noqa: E402

SEG = {"сегмент": "Электрооборудование", "порядок": 2, "направление": "рост",
       "механизм": "m", "секторы": ["Industrials"], "индустрии": []}
UNI = {"liquidity_filter": {"min_avg_daily_volume": 100000}}


def _page(rows):
    return {"data": rows}


def _row(code, vol=500000, mcap=1e9, sector="Industrials", industry="Electrical Equipment & Parts"):
    return {"code": code, "sector": sector, "industry": industry,
            "market_capitalization": mcap, "avgvol_200d": vol}


def _fake_fetch(pages):
    """Фейк HTTP: очередь страниц по порядку вызовов."""
    calls = []

    def fetch(url):
        calls.append(url)
        return pages[len(calls) - 1] if len(calls) <= len(pages) else _page([])
    fetch.calls = calls
    return fetch


def _mem_db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, adjusted_close REAL,"
                " volume INTEGER)")
    con.execute("CREATE TABLE fundamentals (symbol TEXT PRIMARY KEY, name TEXT, sector TEXT,"
                " industry TEXT, market_cap_mln REAL)")
    return con


def test_screener_screen_liquidity_and_normalization(tmp_path):
    fetch = _fake_fetch([_page([_row("AAA"), _row("bbb", vol=50000), _row("CCC", mcap=5e9)])])
    r = SS.screen_segment(SEG, api_key="k", universe=UNI, fetch=fetch, max_instruments=10)
    syms = [i["symbol"] for i in r["инструменты"]]
    assert r["источник"] == "eodhd_screener"
    assert syms == ["CCC.US", "AAA.US"]            # неликвид отсеян; сорт по mcap; SYMBOL.US
    assert all(i["avg_volume"] >= 100000 for i in r["инструменты"])


def test_screener_pagination_and_cap():
    page1 = _page([_row(f"S{i:03d}") for i in range(100)])
    page2 = _page([_row(f"T{i:03d}") for i in range(100)])
    fetch = _fake_fetch([page1, page2])
    r = SS.screen_segment(SEG, api_key="k", universe=UNI, fetch=fetch, max_instruments=150)
    assert len(r["инструменты"]) == 150            # кэп события/сегмента соблюдён
    assert len(fetch.calls) == 2


def test_quota_error_alerts_owner_and_falls_back_db(tmp_path):
    def fetch(url):
        raise SS.QuotaError("HTTP 402 screener EODHD — квота/оплата")
    con = _mem_db()
    notices = tmp_path / "notices.jsonl"
    r = SS.screen_segment(SEG, api_key="k", con=con, universe=UNI, fetch=fetch,
                          notices_path=notices)
    assert "fundamentals_db" in r["источник"]
    lines = notices.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1                          # алерт владельцу — РОВНО append (курсор бота цел)
    rec = json.loads(lines[0])
    assert "квота EODHD" in rec["text"] and rec["ts"]


def test_quota_error_in_http200_body_alerts_and_falls_back(tmp_path):
    """Э4-ревью (HIGH): квотная ошибка в ТЕЛЕ HTTP-200 JSON ({"error":"quota exceeded"}) больше НЕ
    маскируется под пустой скрин — детект тела → QuotaError → алерт владельцу + фолбэк БД."""
    con = _mem_db()
    notices = tmp_path / "notices.jsonl"
    for body in ({"error": "quota exceeded"}, {"message": "payment required"}, {"error": "limit reached"}):
        n0 = len(notices.read_text(encoding="utf-8").splitlines()) if notices.exists() else 0
        r = SS.screen_segment(SEG, api_key="k", con=con, universe=UNI,
                              fetch=lambda u, b=body: b, notices_path=notices)
        assert "fundamentals_db" in r["источник"]        # честный откат на БД
        lines = notices.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n0 + 1                       # РОВНО один append на каждую квоту (курсор цел)
        assert "квота EODHD" in json.loads(lines[-1])["text"]


def test_industry_typo_degrades_to_sector_screener():
    """Э4-ревью (medium): невалидная «индустрия» карты (пустой industry-скрин) деградирует на сектор."""
    seg = {"сегмент": "x", "порядок": 2, "направление": "рост", "механизм": "m",
           "секторы": ["Industrials"], "индустрии": ["Опечатка Industry"]}

    def fetch(url):
        return _page([]) if "industry" in url else _page([_row("AAA")])   # industry пуст → sector даёт
    r = SS.screen_segment(seg, api_key="k", universe=UNI, fetch=fetch, max_instruments=10)
    assert [i["symbol"] for i in r["инструменты"]] == ["AAA.US"]
    assert "деградация" in r["источник"]                  # честная причина деградации


def test_industry_typo_degrades_to_sector_db():
    """Деградация industry→sector и в БД-фолбэке (нет api_key)."""
    con = _mem_db()
    con.execute("INSERT INTO fundamentals VALUES ('VRT.US','Vertiv','Industrials',"
                "'Electrical Equipment & Parts', 5000)")
    for i in range(40):
        con.execute("INSERT INTO quotes VALUES ('VRT.US', ?, 100, 100, 2000000)", (f"2026-06-{i%28+1:02d}",))
    seg = {"сегмент": "x", "порядок": 2, "направление": "рост", "механизм": "m",
           "секторы": ["Industrials"], "индустрии": ["Несуществующая Industry"]}
    r = SS.screen_segment(seg, api_key=None, con=con, universe=UNI)
    assert [i["symbol"] for i in r["инструменты"]] == ["VRT.US"]   # industry пуст → сектор нашёл
    assert "деградация" in r["источник"]


def test_db_fallback_sector_industry_and_liquidity():
    con = _mem_db()
    con.execute("INSERT INTO fundamentals VALUES ('VRT.US','Vertiv','Industrials',"
                "'Electrical Equipment & Parts', 50000)")
    con.execute("INSERT INTO fundamentals VALUES ('CLF.US','Cliffs','Basic Materials','Steel', 8000)")
    con.execute("INSERT INTO fundamentals VALUES ('THIN.US','Thin','Industrials','Electrical Equipment & Parts', 10)")
    for i in range(40):
        con.execute("INSERT INTO quotes VALUES ('VRT.US', ?, 100, 100, 2000000)", (f"2026-06-{i%28+1:02d}",))
        con.execute("INSERT INTO quotes VALUES ('THIN.US', ?, 5, 5, 100)", (f"2026-06-{i%28+1:02d}",))
    r = SS.screen_segment(SEG, api_key=None, con=con, universe=UNI)
    syms = [i["symbol"] for i in r["инструменты"]]
    assert syms == ["VRT.US"]                       # сектор совпал + ликвиден; THIN неликвиден; CLF чужой сектор
    assert "fundamentals_db" in r["инструменты"][0]["источник_скрина"]


def test_db_fallback_empty_is_honest_refusal():
    con = _mem_db()
    r = SS.screen_segment(SEG, api_key=None, con=con, universe=UNI)
    assert r["инструменты"] == [] and "нет данных скрина" in r["отказ"]


def test_annotate_sealable_gate():
    con = _mem_db()
    for i in range(25):
        con.execute("INSERT INTO quotes VALUES ('VRT.US', ?, 100, 100, 1000)", (f"2026-06-{i%28+1:02d}",))
    rows = [{"symbol": "VRT.US"}, {"symbol": "NOHIST.US"}]
    SS.annotate_sealable(rows, con=con)
    assert rows[0]["sealable"] is True and rows[1]["sealable"] is False


def test_screener_available_probe():
    ok, detail = SS.screener_available("k", fetch=lambda u: _page([_row("AAA")]))
    assert ok and detail == "ok"
    def boom(url):
        raise RuntimeError("HTTP 403 screener EODHD")
    ok, detail = SS.screener_available("k", fetch=boom)
    assert not ok and "403" in detail


def test_min_avg_daily_volume_from_universe():
    assert SS.min_avg_daily_volume({"liquidity_filter": {"min_avg_daily_volume": 7}}) == 7
    assert SS.min_avg_daily_volume({}) == 100000    # консервативный фолбэк §14
