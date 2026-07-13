# -*- coding: utf-8 -*-
"""Тесты Э4(д)(ж) + заглушки (в)(г) — конвейер «перебора мира» (orchestrator/world_enum.py).
Герметично: in-memory БД, tmp-журналы/реестры, фейк-fetch. Боевые журналы не трогаются."""
import datetime
import json
import pathlib
import sqlite3
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import world_enum as WE          # noqa: E402
from orchestrator import edge_forward as EFW       # noqa: E402

NOW = datetime.datetime(2026, 7, 13, 9, 0, tzinfo=datetime.timezone.utc)
UNI = {"liquidity_filter": {"min_avg_daily_volume": 100000}}
LIMITS = {"world_enum": {"max_attempts_per_event": 5, "target_instruments_min": 2,
                         "target_instruments_max": 10, "map_ttl_days": 28},
          "per_run_token_budget_usd": {"world_map": 3.0}}

MAP_DOC = {"карта": {"событие": "e", "сегменты": [
    {"сегмент": "Электрооборудование", "порядок": 2, "направление": "рост", "канал": "capex",
     "механизм": "спрос на сети", "секторы": ["Industrials"], "индустрии": []}],
    "обоснование": "фикстура", "уверенность": "средняя"},
    "отказ": None, "провенанс": {"источник": "фикстура-тест"}}

EVENT = {"событие": "e", "ключи": ["k"], "источник_шока": "SRC.US",
         "shock": 0.05, "дата": "2026-07-13"}


def _mem_db(with_hist=("GOOD.US", "SRC.US")):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, adjusted_close REAL,"
                " volume INTEGER)")
    for sym in with_hist:
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


def test_event_stop_no_shock(tmp_path):
    p = WE.enumerate_event({**EVENT, "shock": None}, map_doc=MAP_DOC, con=_mem_db(),
                           universe=UNI, limits=LIMITS, now_dt=NOW)
    assert "шок источника не подтверждён" in p["стоп_события"]
    assert p["перечислено_инструментов"] == 0 and p["пары"] == []


def test_event_stop_empty_map(tmp_path):
    md = {"карта": None, "отказ": "карта пуста: переноса нет", "провенанс": {}}
    p = WE.enumerate_event(EVENT, map_doc=md, con=_mem_db(), universe=UNI,
                           limits=LIMITS, now_dt=NOW)
    assert "карта пуста" in p["стоп_события"]
    assert p["карта_провенанс"] == {}              # провенанс карты в протоколе даже при стопе


def test_instrument_returns_classified_and_next_taken(tmp_path):
    """(д): не-sealable инструмент → возврат с категорией и причиной ДОСЛОВНО, конвейер идёт дальше."""
    con = _mem_db(with_hist=("GOOD.US", "SRC.US"))
    p = WE.enumerate_event(EVENT, map_doc=MAP_DOC, con=con, universe=UNI, limits=LIMITS,
                           api_key="k", fetch=_fetch_rows(["NOHIST", "GOOD"]), now_dt=NOW)
    assert p["перечислено_инструментов"] == 2
    assert [x["инструмент"] for x in p["пары"]] == ["GOOD.US"]   # после отказа взят следующий
    bad = [v for v in p["возвраты"] if v["кандидат"] == "NOHIST.US"]
    assert bad and bad[0]["категория"] == "инструмент" and "не sealable" in bad[0]["причина"]
    assert p["отсев_по_критериям"]                  # attrition-таблица заполнена


def test_source_self_loop_rejected(tmp_path):
    con = _mem_db(with_hist=("SRC.US",))
    p = WE.enumerate_event(EVENT, map_doc=MAP_DOC, con=con, universe=UNI, limits=LIMITS,
                           api_key="k", fetch=_fetch_rows(["SRC"]), now_dt=NOW)
    assert p["пары"] == []
    assert any("самопетля" in v["причина"] for v in p["возвраты"])


def test_attempts_cap_stops_event(tmp_path):
    con = _mem_db(with_hist=())
    codes = [f"C{i:02d}" for i in range(9)]
    p = WE.enumerate_event(EVENT, map_doc=MAP_DOC, con=con, universe=UNI, limits=LIMITS,
                           api_key="k", fetch=_fetch_rows(codes), now_dt=NOW)
    assert p["кэп_достигнут"] is True and p["попыток"] == 5      # кэп из config (рамка 3)
    assert any("кэп попыток" in v["причина"] for v in p["возвраты"])


def test_empty_screen_is_instrument_class_return(tmp_path):
    con = _mem_db(with_hist=())
    p = WE.enumerate_event(EVENT, map_doc=MAP_DOC, con=con, universe=UNI, limits=LIMITS,
                           api_key=None, now_dt=NOW)             # фолбэк БД: fundamentals нет
    v = p["возвраты"][0]
    assert v["категория"] == "инструмент" and v["кандидат"].startswith("сегмент:")
    assert "нет данных скрина" in v["причина"]


def test_event_budget_visible_and_capped(tmp_path):
    p = WE.enumerate_event(EVENT, map_doc=MAP_DOC, con=_mem_db(), universe=UNI,
                           limits=LIMITS, api_key="k", fetch=_fetch_rows(["GOOD"]), now_dt=NOW)
    b = p["бюджет_события"]
    assert b["cap_usd"] == 3.0 and b["spent_usd"] == 0.0 and b["вызовов_llm"] == 0


def test_stubs_wait_for_d3():
    with pytest.raises(NotImplementedError, match="Д3"):
        WE.score_pair_conditional({"источник": "A", "инструмент": "B"})
    with pytest.raises(NotImplementedError, match="Д3"):
        WE.rank_pair({"источник": "A", "инструмент": "B"})


# ── (ж) кандидат-рёбра ────────────────────────────────────────────────────────────
def _sens_yaml(tmp_path, entries):
    p = tmp_path / "sens.yaml"
    p.write_text(yaml.safe_dump({"sensitivities": entries}, allow_unicode=True), encoding="utf-8")
    return p


def test_append_edge_candidates_with_provenance_and_dedup(tmp_path):
    sens = _sens_yaml(tmp_path, [{"источник": "SRC.US", "узел": "INLIB.US", "lag": 0}])
    cand = tmp_path / "cand.jsonl"
    pairs = [{"источник": "SRC.US", "инструмент": "NEW.US", "событие": "e",
              "сегмент": "s", "порядок": 3, "механизм": "m"},
             {"источник": "SRC.US", "инструмент": "INLIB.US", "событие": "e",
              "сегмент": "s", "порядок": 2, "механизм": "m"},        # дубль библиотеки
             {"источник": "SRC.US", "инструмент": "NEW.US", "событие": "e2",
              "сегмент": "s2", "порядок": 2, "механизм": "m2"}]      # дубль кандидата
    r = WE.append_edge_candidates(pairs, path=cand, sens_path=sens, now_dt=NOW, run_id="we_t")
    assert r == {"added": 1, "dup_library": 1, "dup_candidates": 1,
                 "рёбра": ["SRC.US->NEW.US@lag0"]}
    rec = json.loads(cand.read_text(encoding="utf-8").splitlines()[0])
    assert rec["origin"] == "world_enum" and rec["событие"] == "e" and rec["механизм"] == "m"
    assert rec["lag"] == 0 and "провенанс" in rec
    # повторный append — идемпотентен (append-only, без перезаписи)
    r2 = WE.append_edge_candidates(pairs[:1], path=cand, sens_path=sens, now_dt=NOW)
    assert r2["added"] == 0 and r2["dup_candidates"] == 1
    assert len(cand.read_text(encoding="utf-8").splitlines()) == 1


def test_edge_library_merges_candidates_marked_origin(tmp_path):
    sens = _sens_yaml(tmp_path, [{"источник": "A.US", "узел": "B.US", "lag": 0}])
    cand = tmp_path / "cand.jsonl"
    cand.write_text(
        json.dumps({"from": "A.US", "to": "C.US", "lag": 0, "edge_key": "A.US->C.US@lag0"}) + "\n"
        + json.dumps({"from": "A.US", "to": "B.US", "lag": 0}) + "\n"      # дубль библиотеки
        + "мусорная строка\n", encoding="utf-8")
    lib = EFW.edge_library(sens, candidates_path=cand)
    assert {(e["from"], e["to"], e["origin"]) for e in lib} == {
        ("A.US", "B.US", "library"), ("A.US", "C.US", "world_enum")}
    # candidates_path=None → только библиотека (для дедупа в самом world_enum)
    assert len(EFW.edge_library(sens, candidates_path=None)) == 1


def test_pipeline_writes_candidates_when_asked(tmp_path):
    con = _mem_db(with_hist=("GOOD.US", "SRC.US"))
    sens = _sens_yaml(tmp_path, [])
    cand = tmp_path / "cand.jsonl"
    p = WE.enumerate_event(EVENT, map_doc=MAP_DOC, con=con, universe=UNI, limits=LIMITS,
                           api_key="k", fetch=_fetch_rows(["GOOD"]), now_dt=NOW,
                           write_candidates=True, candidates_path=cand, sens_path=sens)
    assert p["кандидат_рёбра"]["added"] == 1
    assert cand.exists()


# ── (е) реестр карт ───────────────────────────────────────────────────────────────
def test_map_registry_and_active_ttl(tmp_path):
    reg = tmp_path / "maps.jsonl"
    WE.register_map(EVENT, MAP_DOC["карта"], 28, ["GOOD.US"], run_id="we_t",
                    path=reg, now_dt=NOW - datetime.timedelta(days=10))
    WE.register_map({**EVENT, "событие": "старое"}, MAP_DOC["карта"], 7, ["X.US"],
                    run_id="we_old", path=reg, now_dt=NOW - datetime.timedelta(days=10))
    act = WE.active_maps(reg, now_dt=NOW)
    assert [m["событие"] for m in act] == ["e"]     # ttl=7 истёк, ttl=28 жив
    assert act[0]["инструменты"] == ["GOOD.US"]
