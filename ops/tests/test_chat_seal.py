# -*- coding: utf-8 -*-
"""Q-чат-в-счёт (11.11): итоговый кандидат разговора → §9-прогноз в счёт ДО сделки, проверки — кодом."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

from ops import chat_seal as CS      # noqa: E402

TODAY = datetime.date(2026, 11, 11)
SNAP = {"last_close": 50.0, "last_date": "2026-11-10", "sigma_daily": 0.02}


def _spec(**kw):
    a = dict(ticker="EQT", direction="above", threshold=58, resolve_by="2026-12-23", today=TODAY, snapshot=SNAP)
    a.update(kw)
    return CS.build_spec(**{k: v for k, v in a.items() if k not in ("ticker", "direction", "threshold", "resolve_by")},
                         ticker=a["ticker"], direction=a["direction"], threshold=a["threshold"], resolve_by=a["resolve_by"])


def test_valid_spec_has_base_and_anchor():
    spec, why = _spec()
    assert why is None and spec["kind"] == "chat_idea" and spec["asset"] == "EQT.US"
    assert spec["якорь_цена"] == 50.0 and spec["ход_проц"] == 16.0 and 0 < spec["вероятность_база"] < 0.5
    assert spec["probability"] is None                          # вероятность не выдумываем


def test_rules_refuse():
    assert "истинной стороне" in _spec(threshold=45)[1]
    assert "недель" in _spec(resolve_by="2026-11-20")[1]
    assert "заякорен" in _spec(threshold=150)[1]
    assert "шум" in _spec(threshold=50.5)[1]
    assert "основания" in _spec(probability=0.7)[1]
    assert _spec(probability=0.7, probability_basis="консенсус не учитывает контракт 05.11; 3 из 4 аналогов")[0]
    assert "нет истории" in _spec(snapshot={"ошибка": "бумаги нет"})[1]


def test_seal_one_open_per_asset(tmp_path):
    pred = tmp_path / "pred.jsonl"
    spec, _ = _spec()
    rec, why = CS.seal(spec, predictions_path=pred, today="2026-11-11")
    assert why is None and rec["hash"]
    spec2, _ = _spec(threshold=60)
    rec2, why2 = CS.seal(spec2, predictions_path=pred, today="2026-11-11")
    assert rec2 is None and "уже идёт прогноз" in why2
    assert "Записал в счёт ДО сделки: EQT.US закроется выше $58.00" in CS.words(rec)


def test_battle_journal_blocked_in_cloud():
    from ops import hunter_seal as HS
    spec, _ = _spec()
    rec, why = CS.seal(spec, predictions_path=HS.BATTLE_PRED)
    assert rec is None and "только на сервере" in why


def test_agent_found_candidate_auto_sealed_and_line_written(tmp_path, monkeypatch):
    import bot_agent as A
    from ops import price_snapshot as PS
    from ops.tests import test_search_queue as T
    monkeypatch.setattr(PS, "lookup", lambda *a, **k: {"last_close": 20.0, "last_date": "2026-11-10",
                                                       "sigma_daily": 0.02})
    tools = A.Tools()
    tools.search_dir = tmp_path / "s"
    tools.pred_path = tmp_path / "pred.jsonl"
    data = T.worthy_data()
    data["горизонт"] = (datetime.date.today() + datetime.timedelta(weeks=6)).isoformat()
    script = [T._msg(calls=[("search_start", {"topic": "медь"})]),
              T._msg(calls=[("search_add", {"thesis": "Переработчики лома получают маржу от разрыва цен", "ticker": "SCHN",
                                            "mechanism": "разрыв цены лома и катодной меди расширяет маржу"})])] + \
             [T._msg(calls=[("search_step", {"hyp_id": "h1", "outcome": "pass", "data": data})]) for _ in range(6)] + \
             [T._msg("Нашёл кандидата.")]
    ag = A.Agent(post=T.FakeLLM(script), tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    text, _f, _c = ag.run("найди идею про медь")
    rows = [json.loads(x) for x in (tmp_path / "pred.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "chat_idea" and rows[0]["threshold"] == 25.0
    assert "Записал в счёт ДО сделки: SCHN.US закроется выше $25.00" in text


def test_find_idea_outcome_without_bet_warns(tmp_path):
    import bot_agent as A
    t = A.Tools()
    t.pred_path = tmp_path / "pred.jsonl"
    out = t.record_find_idea("газ", "идея", "EQT — производитель газа, беру 4 акции, рынок не учёл контракт", ["EQT"])
    assert "не записан в счёт" in out and "record_bet" in out
