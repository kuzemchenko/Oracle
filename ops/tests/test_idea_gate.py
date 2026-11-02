# -*- coding: utf-8 -*-
"""Шлагбаум идеи охотника (ops/idea_gate.py, архитектура 02.11) и печать (ops/hunter_seal.py).

Закрепляют: полная честная карточка проходит; каждый жёсткий критерий валит своей причиной
(катализатор вне окна, мало источников, звено уже отыграно ЧИСЛОМ, слабая асимметрия, порог на
уже-истинной стороне, устаревшая цена, нет стоп-условия); печать пишет отдельный трек
hunter_idea, дубль — «уже запечатано», нет истории цен — честная причина; счёт метода — из
журнала по hash; строка в пуше без вероятностей."""
import copy
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import idea_gate as G          # noqa: E402
import hunter_seal as HS       # noqa: E402

TODAY = datetime.date(2026, 11, 2)

CARD = {
    "ticker": "MP.US", "name": "MP Materials", "direction": "вверх", "zone": "B",
    "mechanism_id": "critical_materials_export_control",
    "catalyst": {"what": "истекает пауза экспортного контроля КНР", "date": "2026-11-10"},
    "thesis": "продления нет — ограничения оживают, западные заместители переоцениваются",
    "facts": [
        {"text": "пауза истекает 10.11.2026", "url": "https://www.jonesday.com/x", "date": "2025-11-12"},
        {"text": "продления нет", "url": "https://finance.yahoo.com/y", "date": "2026-10-30"},
        {"text": "акция у годового дна", "url": "https://www.reuters.com/z", "date": "2026-10-30"},
    ],
    "precedent": {"text": "апрель 2025: лицензии КНР", "amplitude": "+30–60%",
                  "url": "https://www.mining.com/a"},
    "move_since_trigger_pct": -4.0,
    "upside_pct": 30, "downside_pct": 10,
    "who_sells": "моментум-продавцы сектора, принявшие слова о перемирии за документ",
    "kill_conditions": [{"what": "номерная поправка MOFCOM о продлении", "check_by": "2026-11-10"}],
    "spec9": {"asset": "MP.US", "direction": "above", "threshold": 45.0,
              "resolve_by": "2026-12-15", "probability": 0.4,
              "last_close": 39.57, "last_close_date": "2026-10-30"},
}


def _c(**patch):
    c = copy.deepcopy(CARD)
    for k, v in patch.items():
        c[k] = v
    return c


def test_good_card_passes():
    r = G.check(CARD, today=TODAY)
    assert r["pass"], r["fails"]
    assert r["computed"]["асимметрия"] == 3.0 and r["computed"]["горизонт_дней"] == 43


def test_each_hard_criterion_fails_with_reason():
    cases = [
        (_c(catalyst={"what": "x", "date": "2027-06-01"}), "вне окна"),
        (_c(facts=CARD["facts"][:2]), "фактов с URL"),
        (_c(facts=[{**f, "url": "https://www.reuters.com/" + str(i)} for i, f in enumerate(CARD["facts"])]),
         "разных источников"),
        (_c(move_since_trigger_pct=55.0), "уже отыграно"),
        (_c(upside_pct=12, downside_pct=10), "асимметрия"),
        (_c(spec9={**CARD["spec9"], "threshold": 35.0}), "уже-истинной"),
        (_c(spec9={**CARD["spec9"], "last_close_date": "2026-10-01"}), "устарела"),
        (_c(kill_conditions=[]), "kill_conditions"),
        (_c(spec9={**CARD["spec9"], "resolve_by": "2026-11-09"}), "горизонт"),
        (_c(spec9={**CARD["spec9"], "direction": "below", "threshold": 30.0}), "не согласовано"),
        (_c(precedent={"text": "x", "amplitude": "сильно", "url": "https://a.b/c"}), "не распознана"),
    ]
    for card, needle in cases:
        r = G.check(card, today=TODAY)
        assert not r["pass"], needle
        assert any(needle in f for f in r["fails"]), (needle, r["fails"])


def test_cli_exit_codes(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(CARD, ensure_ascii=False), encoding="utf-8")
    assert G.main([str(p), "--today", "2026-11-02"]) == 0
    p.write_text(json.dumps(_c(upside_pct=1), ensure_ascii=False), encoding="utf-8")
    assert G.main([str(p), "--today", "2026-11-02"]) == 1


NAME = f"idea_{datetime.datetime.now(datetime.timezone.utc).date()}_MP.push.txt"


def test_seal_card_separate_track_and_dedup(tmp_path):
    pred = tmp_path / "predictions.jsonl"
    rec, why = HS.seal_card(CARD, idea_file=NAME, predictions_path=pred,
                            sealable_fn=lambda a: True)
    assert why is None and rec["kind"] == "hunter_idea" and rec["track"] == "hunter_idea"
    assert rec["resolve_by"].startswith("2026-12-15") and rec["threshold"] == 45.0
    rec2, why2 = HS.seal_card(CARD, idea_file=NAME, predictions_path=pred, sealable_fn=lambda a: True)
    assert rec2 is None and why2 == "уже запечатано"
    # «обновлённая версия» той же идеи с другим порогом — не вторая ставка (ревью 02.11)
    moved = _c(spec9={**CARD["spec9"], "threshold": 47.0})
    rec2b, why2b = HS.seal_card(moved, idea_file=NAME, predictions_path=pred, sealable_fn=lambda a: True)
    assert rec2b is None and why2b == "уже запечатано"
    rec3, why3 = HS.seal_card(CARD, idea_file=NAME, predictions_path=tmp_path / "p2.jsonl",
                              sealable_fn=lambda a: False)
    assert rec3 is None and "нет истории цен" in why3
    rec4, why4 = HS.seal_card(CARD, idea_file="idea_без_даты.push.txt",
                              predictions_path=tmp_path / "p3.jsonl", sealable_fn=lambda a: True)
    assert rec4 is None and "нет даты" in why4                 # лаг-гард fail-closed


def test_method_score_and_footer_no_probability(tmp_path):
    pred, outc = tmp_path / "p.jsonl", tmp_path / "o.jsonl"
    rec, _ = HS.seal_card(CARD, idea_file=NAME, predictions_path=pred, sealable_fn=lambda a: True)
    outc.write_text(json.dumps({"hash": rec["hash"], "outcome": 1}) + "\n", encoding="utf-8")
    sc = HS.method_score(predictions_path=pred, outcomes_path=outc)
    assert sc == {"идей": 1, "исходов": 1, "попаданий": 1}
    lines = HS.footer(rec, None, sc)
    joined = " ".join(lines)
    assert "MP закроется выше 45 к 15 декабря" in joined and "сбылось 1 из 1" in joined
    assert "0.4" not in joined and "вероятн" not in joined and "шанс" not in joined
    assert "уже записан" in " ".join(HS.footer(None, "уже запечатано", sc))


def test_penny_stock_rejected():
    c = _c(spec9={**CARD["spec9"], "last_close": 3.2, "threshold": 4.0})
    r = G.check(c, today=TODAY)
    assert not r["pass"] and any("копеечная" in f for f in r["fails"])


def test_priced_in_by_idea_direction_and_potential_caps():
    """Ревью 02.11: «в цене» — по направлению ИДЕИ (переменная механизма может идти против бумаги);
    потенциал не больше прецедентного хода; цель прогноза в пределах потенциала."""
    # идея «вниз», прецедент описан положительной амплитудой переменной, бумага уже −55%
    down_played = _c(direction="вниз", move_since_trigger_pct=-55.0,
                     spec9={**CARD["spec9"], "direction": "below", "threshold": 35.0})
    r = G.check(down_played, today=TODAY)
    assert any("уже отыграно" in f for f in r["fails"]), r["fails"]
    # та же идея, бумага пока −4% — не отыграно
    down_fresh = _c(direction="вниз", move_since_trigger_pct=-4.0, upside_pct=20, downside_pct=8,
                    spec9={**CARD["spec9"], "direction": "below", "threshold": 35.0})
    assert G.check(down_fresh, today=TODAY)["pass"], G.check(down_fresh, today=TODAY)["fails"]
    r2 = G.check(_c(upside_pct=90, downside_pct=10), today=TODAY)
    assert any("больше прецедентного хода" in f for f in r2["fails"])
    r3 = G.check(_c(spec9={**CARD["spec9"], "threshold": 60.0}), today=TODAY)
    assert any("больше заявленного потенциала" in f for f in r3["fails"])


def test_server_check_uses_server_price_and_rejects_undated():
    ok, fails, c2 = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                    last_close_fn=lambda a: (46.0, "2026-10-31"))
    assert not ok and any("уже-истинной" in f for f in fails)   # серверная цена выше порога 45
    assert c2["spec9"]["last_close"] == 46.0
    ok2, fails2, _ = HS.server_check(CARD, idea_file="idea_MP.push.txt", today=TODAY)
    assert not ok2 and "нет даты" in fails2[0]
    ok3, _f3, _ = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                  last_close_fn=lambda a: (None, None))
    assert ok3                                                   # нет котировок → цена карточки
