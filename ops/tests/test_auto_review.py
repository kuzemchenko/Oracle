# -*- coding: utf-8 -*-
"""Тесты автопетли §25 (ops/auto_review.py): числа разбора, дозор с дедупом, канал заметок.
Герметично: все пути — во временном каталоге, боевые журналы не читаются и не пишутся."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import auto_review as AR                      # noqa: E402

NOW = datetime.datetime(2026, 7, 9, 21, 15, tzinfo=datetime.timezone.utc)


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _journals(tmp_path, money_seal_day="2026-06-28"):
    """Минимальные журналы: money-печать (давняя), провизорная + её исход, edge_forward печать."""
    preds = _write_jsonl(tmp_path / "predictions.jsonl", [
        {"hash": "m1", "kind": "cascade_money", "asset": "OXY.US",
         "sealed_at": f"{money_seal_day}T09:00:00+00:00", "probability": 0.8},
        {"hash": "p1", "kind": "cascade_provisional", "asset": "FRO.US",
         "sealed_at": "2026-07-05T09:00:00+00:00", "probability": 0.9},
        {"hash": "e1", "kind": "edge_forward", "asset": "GEV.US",
         "sealed_at": "2026-07-05T07:30:00+00:00", "probability": 0.6},
    ])
    outs = _write_jsonl(tmp_path / "outcomes.jsonl", [
        {"hash": "p1", "kind": "cascade_provisional", "asset": "FRO.US", "probability": 0.9,
         "outcome": 0, "direction": "лонг", "resolve_by": "2026-07-08",
         "resolved_at": "2026-07-08T21:00:00+00:00"},
    ])
    funnel = tmp_path / "funnel_logs"
    _write_jsonl(funnel / "ef_1.json", [])  # создаём каталог
    (funnel / "ef_1.json").write_text(json.dumps({
        "ts": "2026-07-08T09:00:01+00:00",
        "внимание_покрытие": {"покрытие": 0.5},
        "граф_отбор": {"запечатано": {"money": 0},
                       "money_трек": [{"актив": "EEM.US"}],
                       "суд_money": {"EEM.US": {"исход": "РАЗБИТА", "балл": 0.83,
                                                "почему_возможность": "r2=0"}}},
    }, ensure_ascii=False), encoding="utf-8")
    promo = tmp_path / "promotions" / "report.json"
    promo.parent.mkdir(parents=True, exist_ok=True)
    promo.write_text(json.dumps({"n_edges": 3, "n_promote": 0, "promotions": {},
                                 "stats": {}}), encoding="utf-8")
    return {"predictions_path": preds, "outcomes_path": outs,
            "funnel_glob": str(funnel / "ef_*.json"), "promo_path": promo}


def test_compute_review_числа(tmp_path, monkeypatch):
    monkeypatch.setattr(AR.SEAL, "read_predictions",
                        lambda p=None: [json.loads(l) for l in open(p, encoding="utf-8")])
    j = _journals(tmp_path)
    r = AR.compute_review(now=NOW, **j)
    assert r["треки"]["money"]["запечатано"] == 1
    assert r["треки"]["money"]["исходов"] == 0
    assert r["треки"]["provisional"]["исходов"] == 1
    assert r["треки"]["provisional"]["brier"] == 0.81      # (0.9-0)^2
    assert r["треки"]["edge_forward"]["запечатано"] == 1 and r["треки"]["edge_forward"]["зреет"] == 1
    assert r["money_засуха_дней"] == 11                     # 28.06 → 09.07
    assert r["суд_7д"]["судов"] == 1 and r["суд_7д"]["разбито"] == 1
    assert r["худшие_промахи_7д"][0]["актив"] == "FRO.US"
    assert r["внимание_покрытие_7д"] == 0.5
    assert r["промоушен"]["n_promote"] == 0


def test_weekly_пишет_отчёт_и_заметку(tmp_path, monkeypatch):
    monkeypatch.setattr(AR.SEAL, "read_predictions",
                        lambda p=None: [json.loads(l) for l in open(p, encoding="utf-8")])
    j = _journals(tmp_path)
    notices = tmp_path / "notices.jsonl"
    AR.run_weekly(notices_path=notices, reports_dir=tmp_path / "rep", now=NOW, **j)
    md = list((tmp_path / "rep").glob("review_*.md"))
    assert md and "Авторазбор §25" in md[0].read_text(encoding="utf-8")
    notice = json.loads(notices.read_text(encoding="utf-8").splitlines()[0])
    assert "авторазбор" in notice["text"].lower()
    assert "монетки" in notice["text"]                      # расшифровка для обывателя


def test_watch_дедуп_состояний(tmp_path, monkeypatch):
    """Дозор: засуха ≥7 дн. алертит ОДИН раз на ступень; суд <4 судов молчит; повтор — тишина."""
    monkeypatch.setattr(AR.SEAL, "read_predictions",
                        lambda p=None: [json.loads(l) for l in open(p, encoding="utf-8")])
    j = _journals(tmp_path)                                 # засуха 11 дн., судов 1 (<4)
    notices = tmp_path / "notices.jsonl"
    state = tmp_path / "state.json"
    r1 = AR.run_watch(notices_path=notices, state_path=state, now=NOW, **j)
    assert r1["засуха_дней"] == 11
    lines = notices.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "ДЕНЕЖНОЙ" in json.loads(lines[0])["text"]
    r2 = AR.run_watch(notices_path=notices, state_path=state, now=NOW, **j)  # повтор — дедуп
    assert len(notices.read_text(encoding="utf-8").splitlines()) == 1
    assert r2["fired"] == []


def test_watch_промоушен_доступен(tmp_path, monkeypatch):
    monkeypatch.setattr(AR.SEAL, "read_predictions",
                        lambda p=None: [json.loads(l) for l in open(p, encoding="utf-8")])
    j = _journals(tmp_path, money_seal_day="2026-07-09")    # засухи нет — изолируем сигнал
    json.dump({"n_edges": 3, "n_promote": 1,
               "promotions": {"VRT.US->GEV.US@lag30": {"promote": True}}, "stats": {}},
              open(j["promo_path"], "w", encoding="utf-8"))
    notices = tmp_path / "notices.jsonl"
    state = tmp_path / "state.json"
    AR.run_watch(notices_path=notices, state_path=state, now=NOW, **j)
    lines = [json.loads(l) for l in notices.read_text(encoding="utf-8").splitlines()]
    assert any("КОРРЕКТИРОВКА ДОСТУПНА" in n["text"] for n in lines)
    assert all("--apply" in n["text"] for n in lines if "КОРРЕКТИРОВКА" in n["text"])
    # повтор без нового отчёта — тишина (дедуп по mtime)
    AR.run_watch(notices_path=notices, state_path=state, now=NOW, **j)
    assert len(notices.read_text(encoding="utf-8").splitlines()) == len(lines)
