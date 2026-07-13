# -*- coding: utf-8 -*-
"""Тесты Э4(е) — еженедельный пере-скрин активных карт мира (ops/rescan_maps.py).
Герметично: tmp-реестр, in-memory БД, фейк-fetch. Сеть/боевые журналы не трогаются."""
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops import rescan_maps as RM                  # noqa: E402
from orchestrator import world_enum as WE          # noqa: E402

NOW = datetime.datetime(2026, 7, 19, 9, 0, tzinfo=datetime.timezone.utc)
UNI = {"liquidity_filter": {"min_avg_daily_volume": 100000}}

КАРТА = {"событие": "e", "сегменты": [
    {"сегмент": "Электрооборудование", "порядок": 2, "направление": "рост",
     "механизм": "m", "секторы": ["Industrials"], "индустрии": []}]}


def _mem_db(hist_syms):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, adjusted_close REAL,"
                " volume INTEGER)")
    for sym in hist_syms:
        for i in range(25):
            con.execute("INSERT INTO quotes VALUES (?, ?, 100, 100, 1000000)",
                        (sym, f"2026-06-{i % 28 + 1:02d}"))
    return con


def _fetch_rows(codes):
    def fetch(url):
        return {"data": [{"code": c, "sector": "Industrials", "industry": "X",
                          "market_capitalization": 1e9 - i, "avgvol_200d": 500000}
                         for i, c in enumerate(codes)]}
    return fetch


def test_rescan_diff_added_and_dropped(tmp_path):
    reg = tmp_path / "maps.jsonl"
    event = {"событие": "e", "источник_шока": "SRC.US"}
    WE.register_map(event, КАРТА, 28, ["OLD.US", "KEEP.US"], run_id="we_t", path=reg,
                    now_dt=NOW - datetime.timedelta(days=6))
    con = _mem_db(["KEEP.US", "NEW.US"])           # OLD.US потерял историю; NEW.US добрал
    p = RM.rescan(registry_path=reg, api_key="k", con=con, universe=UNI,
                  fetch=_fetch_rows(["KEEP", "NEW", "OLD"]), write=False, now_dt=NOW)
    assert p["активных_карт"] == 1
    d = p["карты"][0]["дифф"]
    assert d["добавились"] == ["NEW.US"] and d["выпали"] == ["OLD.US"]
    assert d["было"] == 2 and d["стало"] == 2      # KEEP остался, NEW пришёл (sealable-гейт жив)


def test_rescan_excludes_shock_source_and_applies_quota(tmp_path):
    """Э4-ревью (medium): пере-скрин ВОСПРОИЗВОДИТ правила enumerate_event — источник шока
    исключается из текущих, и на сегмент действует квота (а не кэп 300). Иначе дифф несравним."""
    reg = tmp_path / "maps.jsonl"
    event = {"событие": "e", "источник_шока": "SRC.US"}
    WE.register_map(event, КАРТА, 28, ["KEEP.US"], run_id="we_t", path=reg,
                    now_dt=NOW - datetime.timedelta(days=6))
    con = _mem_db(["KEEP.US", "NEW.US", "SRC.US"])         # у источника тоже есть история
    # скрин возвращает и источник, и новый инструмент — источник не должен попасть в «текущие»
    p = RM.rescan(registry_path=reg, api_key="k", con=con, universe=UNI,
                  fetch=_fetch_rows(["KEEP", "NEW", "SRC"]), write=False, now_dt=NOW)
    d = p["карты"][0]["дифф"]
    assert "SRC.US" not in d["добавились"] and "SRC.US" not in (set(["KEEP.US"]) | set(d["добавились"]))
    assert d["добавились"] == ["NEW.US"]                    # источник исключён, новый добавлен
    seg0 = p["карты"][0]["сегменты"][0]
    assert "квота" in seg0                                  # квота присутствует (правила enumerate)


def test_rescan_skips_expired_maps(tmp_path):
    reg = tmp_path / "maps.jsonl"
    WE.register_map({"событие": "старое"}, КАРТА, 7, ["X.US"], run_id="we_old", path=reg,
                    now_dt=NOW - datetime.timedelta(days=10))
    p = RM.rescan(registry_path=reg, api_key=None, con=_mem_db([]), universe=UNI,
                  write=False, now_dt=NOW)
    assert p["активных_карт"] == 0 and p["карты"] == []


def test_rescan_empty_registry_ok(tmp_path):
    p = RM.rescan(registry_path=tmp_path / "нет.jsonl", api_key=None, con=_mem_db([]),
                  universe=UNI, write=False, now_dt=NOW)
    assert p["активных_карт"] == 0                 # пустой реестр — штатный (до Э5 карты не пишутся)
