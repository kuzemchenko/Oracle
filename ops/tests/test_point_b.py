# -*- coding: utf-8 -*-
"""Замер точки Б (ops/point_b.py): только чтение, счёт из журналов (аудит 03.11 P0)."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import point_b as PB   # noqa: E402


def _w(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def test_measure_counts_from_journals_only(tmp_path):
    j = tmp_path / "journal"
    _w(j / ("predictions" + ".jsonl"), [
        {"kind": "hunter_idea", "hash": "h1", "sealed_at": "2026-11-03T06:18:41+00:00", "probability": 0.15,
         "вероятность_база": 0.07, "resolve_by": "2026-11-25", "mechanism_id": "m1"},
        {"kind": "thesis_event", "hash": "t1", "sealed_at": "2026-11-05T09:00:00+00:00", "probability": 0.4,
         "вероятность_база": 0.3, "resolve_by": "2026-12-04", "thesis_id": "T1"}])
    _w(j / ("outcomes" + ".jsonl"), [{"kind": "thesis_event", "hash": "t1", "outcome": 1}])
    _w(j / "deliveries.jsonl", [
        {"ts": "2026-11-04T12:10:00+00:00", "kind": "идея охотника"},
        {"ts": "2026-11-05T16:30:00+00:00", "kind": "идея охотника"},       # после 15:00 — не день с идеей
        {"ts": "2026-11-06T14:00:00+00:00", "kind": "честный ноль"},
        {"ts": "2026-11-07T12:00:00+00:00", "kind": "идея охотника"},       # суббота — не считается
        {"ts": "2026-11-06T00:01:00+00:00", "kind": "бот жив"}])
    _w(j / "deep_dive.jsonl", [{"run_id": "h_x", "asset": "MP.US", "stage": "day2"}])
    _w(tmp_path / "knowledge" / "edge_candidates.jsonl", [{"origin": "thesis"}, {"origin": "mechanism"}])
    (tmp_path / "reports" / "ideas" / "claims").mkdir(parents=True)
    (tmp_path / "reports" / "ideas" / "claims" / "2026-11-05.txt").write_text("запрос", encoding="utf-8")   # закрыт 06.11 нолём
    (tmp_path / "reports" / "ideas" / "claims" / "2026-11-08.txt").write_text("запрос", encoding="utf-8")   # не закрыт
    (tmp_path / "reports" / "machinist").mkdir(parents=True)
    (tmp_path / "reports" / "machinist" / "friday_2026-11-06.md").write_text("x", encoding="utf-8")
    m = PB.measure("2026-11-08", root=tmp_path)
    c = {x["что"]: x["сейчас"] for x in m["критерии"]}
    assert m["дни_с_идеей"] == ["2026-11-03", "2026-11-04", "2026-11-06"]   # 03.11 — по печати до журнала
    assert m["запросы"] == {"всего": 2, "закрыто": 1, "незакрытые": ["2026-11-08"]}
    assert c["полный цикл прокачки «Качаем 2 дня» дошёл до паспорта"] == 1
    assert c["событийный трек: запечатано вопросов"] == 1 and c["событийный трек: сверено исходов"] == 1
    assert c["связи новых классов под наблюдением (с происхождением)"] == 2
    assert c["пятничные отчёты машиниста"] == 1
    assert c["идеи охотника доставлены (формат одобрен владельцем)"] == 4    # 3 доставки + 1 печать до журнала
    assert m["событийный_трек"]["умение"]["verdict"] == "данных мало"
    md = PB.render_md(m)
    assert "| Б1 | дни с идеей" in md and "данных мало" in md and "не инвестиционная рекомендация" in md



def test_discussion_idea_closes_request_but_is_not_a_recorded_hunter_push(tmp_path):
    """Поправка владельца 04.11: идея «для обсуждения» закрывает запрос «найди идею», но в критерий
    «идеи охотника доставлены» (записанные в счёт) не входит."""
    import json
    from ops import point_b as PB
    (tmp_path / "journal").mkdir()
    (tmp_path / "reports" / "ideas" / "claims").mkdir(parents=True)
    (tmp_path / "reports" / "ideas" / "claims" / "2026-11-03.txt").write_text("x", encoding="utf-8")
    (tmp_path / "journal" / "deliveries.jsonl").write_text(json.dumps(
        {"ts": "2026-11-04T16:10:00+00:00", "kind": "идея для обсуждения", "ref": "idea_2026-11-03_DHT.push.txt"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    m = PB.measure(today="2026-11-04", root=tmp_path)
    assert m["запросы"] == {"всего": 1, "закрыто": 1, "незакрытые": []}
    hunter = [c for c in m["критерии"] if c["что"].startswith("идеи охотника доставлены")][0]
    assert hunter["сейчас"] == 0
