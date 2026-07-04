# -*- coding: utf-8 -*-
"""Тесты построения §9-прогноза и запечатывания форвард-идей (orchestrator/forecast).

Проверяет (инвариант 3 CLAUDE.md, скилл run-funnel п.6):
  - идея поля суждений → §9-разрешимый прогноз КОДОМ (порог = текущий close, не выдумка);
  - направление лонг/шорт → above/below; неразрешимая идея честно НЕ запечатывается (П8);
  - запечатывание идёт в ПЕРЕДАННЫЙ путь (тест НЕ трогает боевой journal/predictions.jsonl, П16);
  - stage6 на mock НЕ запечатывает, на seal_predictions=True — запечатывает каждую выданную идею
    ДО сборки отчёта; хэш верифицируется."""
import datetime
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import forecast as FC      # noqa: E402
from mathlib import sealing as SEAL          # noqa: E402

NOW = datetime.datetime(2026, 6, 12, 13, 0, 0, tzinfo=datetime.timezone.utc)
_CTX = {"quotes": {"BNO.US": {"last": {"close": 51.46}, "last_date": "2026-06-10"}}}


def test_direction_mapping():
    assert FC.direction_to_side("лонг") == "above"
    assert FC.direction_to_side("шорт") == "below"
    assert FC.direction_to_side("непонятно") is None


def test_build_prediction_resolvable_and_code_sourced():
    cand = {"актив": "BNO.US", "направление": "лонг", "тезис": "нефть растёт",
            "вероятность_судьи": 0.58, "горизонт": "2 недели", "школа": "b_causal_links"}
    pred, reason = FC.build_forward_prediction(cand, _CTX, run_id="t", kind="funnel_forward",
                                               now_dt=NOW, probability=0.58)
    assert reason == "ok"
    assert pred["direction"] == "above"
    assert pred["threshold"] == 51.46            # = текущий close, не выдумка
    assert pred["probability"] == 0.58
    assert pred["resolve_by"] == "2026-06-26T20:00:00+00:00"   # +10 торг.дней ≈ +14 кал.
    assert not SEAL.validate_resolvable(pred)     # §9-разрешим


def test_unsealable_when_no_price_or_direction():
    p1, r1 = FC.build_forward_prediction({"актив": "ZZZ.US", "направление": "лонг"}, _CTX,
                                         run_id="t", kind="x", now_dt=NOW)
    assert p1 is None and "нет цены" in r1
    p2, r2 = FC.build_forward_prediction({"актив": "BNO.US", "направление": "?"}, _CTX,
                                         run_id="t", kind="x", now_dt=NOW)
    assert p2 is None and "направление" in r2


def test_seal_writes_only_to_given_path(tmp_path):
    cand = {"актив": "BNO.US", "направление": "шорт", "вероятность_судьи": 0.4}
    pred, _ = FC.build_forward_prediction(cand, _CTX, run_id="t", kind="calibration", now_dt=NOW)
    jpath = tmp_path / "p.jsonl"
    rec = FC.seal_prediction(pred, path=str(jpath))
    assert SEAL.verify_seal(rec)
    assert rec["direction"] == "below"
    lines = jpath.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1                        # ровно один append


def test_stage6_seals_before_report(tmp_path):
    from orchestrator import funnel as F, context as C, openrouter as OR, synthesis as SY
    ctx = C.build_context(theme="brent")
    client = OR.make_client(mode="mock", run_id="t")
    costs = SY.load_costs()
    from mathlib import portfolio as PF
    limits = PF.lim.load_limits()
    cand = {"актив": "BNO.US", "направление": "лонг", "тезис": "тест",
            "вероятность_судьи": 0.55, "школа": "b_causal_links",
            "_debate": {"вердикт": "ПОДТВЕРЖДЕНО",
                        "реплики": {"критик": {"ok": True, "judgment": {}},
                                    "судья": {"ok": True, "judgment": {}}}}}
    jpath = tmp_path / "p.jsonl"
    synth = F.stage6_synthesis([cand], {}, ctx, client, costs, limits,
                               run_id="t", seal_predictions=True, predictions_path=str(jpath),
                               now_dt=NOW)
    rep = synth["отчёты"][0]
    sd = rep["запечатанный_прогноз_§9"]
    assert sd["sealed"] is True and sd["asset"] == "BNO.US"
    recs = SEAL.read_predictions(str(jpath))
    assert len(recs) == 1 and SEAL.verify_seal(recs[0])


def test_direction_negation_refused():
    # M12 (ревью 04.07): «не лонг» больше не превращается в above у ЗАПЕЧАТАННОГО прогноза.
    from orchestrator.forecast import direction_to_side
    assert direction_to_side("не лонг") is None
    assert direction_to_side("not long") is None
    assert direction_to_side("лонг") == "above"
    assert direction_to_side("short") == "below"
