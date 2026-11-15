# -*- coding: utf-8 -*-
"""Правила v3 (knowledge/idea_rules.yaml, владелец 15.11 «Б»): брак — только ФАКТЫ/ТОРГУЕМОСТЬ/ВЫХОД/СЧЁТ."""
import copy
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import idea_gate as G          # noqa: E402
import idea_rules as IR        # noqa: E402
import hunter_court as HC      # noqa: E402
from test_idea_gate import CARD, TODAY   # noqa: E402


def _c(**patch):
    c = copy.deepcopy(CARD)
    c.update(patch)
    return c


def test_file_is_consistent():
    r = IR.load()
    ids = [x["id"] for x in r["брак"] + r["пометки"]]
    assert len(ids) == len(set(ids))
    assert {x["id"] for x in r["брак"]} == {"ФАКТЫ", "ТОРГУЕМОСТЬ", "ВЫХОД", "СЧЁТ"}
    assert all(x.get("словами") for x in r["пометки"])
    assert IR.kind("НЕВЕДОМОЕ") == IR.HARD                    # опечатка в коде не ослабляет проверку


def test_soft_rules_become_notes_only_under_v3():
    weak = _c(target_price=41.5, upside_pct=round((41.5 / 39.57 - 1) * 100, 2), precedent={"text": "x", "amplitude": "+30%"},
              spec9={**CARD["spec9"], "threshold": 41.0})            # порог — в пределах цели (иначе брак СЧЁТ)
    old = G.check(weak, today=TODAY, rules_v3=False)
    assert not old["pass"] and any("асимметрия" in f for f in old["fails"])
    new = G.check(weak, today=TODAY, rules_v3=True)
    assert new["pass"], new["fails"]
    assert any(w.startswith("пометка (асимметрия)") for w in new["warnings"])
    assert any(w.startswith("пометка (прецедент)") for w in new["warnings"])
    tags = {t for t, _ in new["computed"]["по_правилам"]}
    assert {"АСИММЕТРИЯ", "ПРЕЦЕДЕНТ"} <= tags
    assert "возможный выигрыш меньше чем в полтора раза" in "; ".join(
        IR.weak_points(new["computed"]["по_правилам"]))


def test_hard_rules_still_kill_under_v3():
    few_facts = _c(facts=CARD["facts"][:1])
    assert not G.check(few_facts, today=TODAY, rules_v3=True)["pass"]                    # ФАКТЫ (П8)
    no_exit = _c(kill_conditions=[])
    assert not G.check(no_exit, today=TODAY, rules_v3=True)["pass"]                      # ВЫХОД
    penny = _c(spec9={**CARD["spec9"], "last_close": 3.0, "threshold": 3.5}, target_price=4.0, stop_price=2.7,
               trigger_close=3.1, move_since_trigger_pct=round((3 / 3.1 - 1) * 100, 2),
               upside_pct=round((4 / 3 - 1) * 100, 2), downside_pct=10)
    r = G.check(penny, today=TODAY, rules_v3=True)
    assert not r["pass"] and any("копеечная" in f for f in r["fails"])                  # ТОРГУЕМОСТЬ
    bad_spec = _c(spec9={**CARD["spec9"], "resolve_by": "2026-11-05"})
    assert not G.check(bad_spec, today=TODAY, rules_v3=True)["pass"]                     # СЧЁТ


def test_missing_soft_field_does_not_stop_check_under_v3():
    c = _c()
    c.pop("precedent")
    r = G.check(c, today=TODAY, rules_v3=True)
    assert r["pass"], r["fails"]
    assert any("«precedent»" in w for w in r["warnings"])
    assert not G.check(c, today=TODAY, rules_v3=False)["pass"]


def test_v3_off_until_activation_date():
    rules = {"действует_с": None}
    assert not IR.active(TODAY, rules)
    assert IR.active(TODAY, {"действует_с": "2026-11-01"}) and not IR.active(TODAY, {"действует_с": "2026-11-03"})


def test_court_kills_only_allowed_points():
    card_text = "бизнес компании не связан с поводом вообще никак"
    j = '{"verdict": "FATAL", "point": 2, "evidence": "бизнес компании не связан с поводом", "objection": "x"}'
    assert HC.parse_verdict(j, card_text)["verdict"] == "FATAL"                  # прежние правила: 1–4
    assert HC.parse_verdict(j, card_text, fatal_points=(1,))["verdict"] == "FIX"  # v3: пункт 2 — доработка
    j1 = j.replace('"point": 2', '"point": 1')
    assert HC.parse_verdict(j1, card_text, fatal_points=(1,))["verdict"] == "FATAL"
    assert "FATAL" in IR.text_for_prompt() or "убить" in IR.text_for_prompt()
