# -*- coding: utf-8 -*-
"""Тесты mock end-to-end обвязки Э4 (orchestrator/world_enum_dryrun.py).
Герметично: tmp-файловая БД (read-only открытие как в бою), фикстура карты из репо,
API не дёргается (api_key=None → фолбэк Tier0-фундаментал БД)."""
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import world_enum_dryrun as WD   # noqa: E402

NOW = datetime.datetime(2026, 7, 13, 9, 0, tzinfo=datetime.timezone.utc)


def _file_db(tmp_path):
    """Файловая БД: quotes (VRT.US с шоком + пара терминалов) + fundamentals для фолбэка скрина."""
    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, adjusted_close REAL,"
                " volume INTEGER)")
    con.execute("CREATE TABLE fundamentals (symbol TEXT PRIMARY KEY, name TEXT, sector TEXT,"
                " industry TEXT, market_cap_mln REAL)")
    def series(sym, closes):
        for i, c in enumerate(closes):
            con.execute("INSERT INTO quotes VALUES (?, ?, ?, ?, 2000000)",
                        (sym, f"2026-{5 + i // 28:02d}-{i % 28 + 1:02d}", c, c))
    series("VRT.US", [100.0] * 25 + [106.0] * 5)   # шок источника внутри окна реакции
    series("GEV.US", [50.0] * 30)
    series("CLF.US", [20.0] * 30)
    series("NOHIST.US", [10.0] * 10)               # ликвиден по объёму, но истории < 20 баров
    for sym, sec, ind in (("VRT.US", "Industrials", "Electrical Equipment & Parts"),
                          ("GEV.US", "Industrials", "Specialty Industrial Machinery"),
                          ("CLF.US", "Basic Materials", "Steel"),
                          ("NOHIST.US", "Industrials", "Electrical Equipment & Parts")):
        con.execute("INSERT INTO fundamentals VALUES (?, ?, ?, ?, 1000)", (sym, sym, sec, ind))
    con.commit()
    con.close()
    return db


def test_dryrun_end_to_end_offline(tmp_path):
    db = _file_db(tmp_path)
    p = WD.dry_run(db=db, api_key=None, write=False, now_dt=NOW)
    assert p.get("стоп_события") is None
    assert p["событие"]["источник_шока"] == "VRT.US"
    assert p["событие"]["shock"] is not None and p["событие"]["shock"] > 0
    # фикстурная карта: 5 сегментов, провенанс фикстуры в протоколе
    assert len(p["карта"]["сегменты"]) == 5
    assert "фикстура" in str(p["карта_провенанс"])
    # перечисление живёт на фолбэке БД; отказы классифицированы
    assert p["перечислено_инструментов"] >= 3
    принятые = {x["инструмент"] for x in p["пары"]}
    assert "GEV.US" in принятые and "CLF.US" in принятые
    assert "VRT.US" not in принятые                # самопетля источника отброшена
    отказ_nohist = [v for v in p["возвраты"] if v["кандидат"] == "NOHIST.US"]
    assert отказ_nohist and отказ_nohist[0]["категория"] == "инструмент"
    # бюджет события закэпован и ВИДЕН (фикстура → LLM-вызовов 0)
    assert p["бюджет_события"]["cap_usd"] > 0 and p["бюджет_события"]["вызовов_llm"] == 0
    # у каждой пары есть механизм — корм (ж)
    assert all(x["механизм"] for x in p["пары"])
    # заглушки (в)(г) задекларированы
    assert "Д3" in p["скоринг_ранг"]


def test_dryrun_db_opened_read_only(tmp_path):
    """Каркас не имеет права писать в боевую БД: соединение дриба — mode=ro."""
    db = _file_db(tmp_path)
    con = WD._connect_ro(db)
    try:
        import pytest
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO quotes VALUES ('X.US','2026-07-13',1,1,1)")
    finally:
        con.close()


def test_dryrun_no_journal_writes(tmp_path, monkeypatch):
    """write=False → ни протокола, ни кандидатов; боевые predictions/outcomes не упоминаются вовсе."""
    db = _file_db(tmp_path)
    cand = tmp_path / "cand.jsonl"
    p = WD.dry_run(db=db, api_key=None, write=False, now_dt=NOW,
                   append_candidates=False, candidates_path=cand)
    assert not cand.exists() and "кандидат_рёбра" not in p


def test_dryrun_append_candidates_opt_in(tmp_path):
    import yaml
    db = _file_db(tmp_path)
    cand = tmp_path / "cand.jsonl"
    sens = tmp_path / "sens.yaml"                  # пустая библиотека — дедуп герметичен
    sens.write_text(yaml.safe_dump({"sensitivities": []}), encoding="utf-8")
    p = WD.dry_run(db=db, api_key=None, write=False, now_dt=NOW,
                   append_candidates=True, candidates_path=cand, sens_path=sens)
    assert p["кандидат_рёбра"]["added"] == p["принято_пар"] > 0
    rec = json.loads(cand.read_text(encoding="utf-8").splitlines()[0])
    assert rec["origin"] == "world_enum" and rec["from"] == "VRT.US"
