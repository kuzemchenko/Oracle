# -*- coding: utf-8 -*-
"""Балл и решение комитета (Q-вердикт-комитет, 22.11) и состояние сектора — чистая математика, без сети."""
from mathlib import idea_score as IS
from mathlib import sector_rules as SR

RULES = {"решения": {"ставка": 8, "ожидание": 6},
         "пункты": [{"id": i, "код": f"П{i}", "словами": f"пункт {i}", "не_зачтён": f"нет пункта {i}"} for i in range(1, 11)]}


def _pts(n_ok, n_none=0):
    P = {}
    for i in range(1, 11):
        P[i] = {"зачтён": True if i <= n_ok else (None if i <= n_ok + n_none else False), "почему": ""}
    return P


def test_score_three_decisions():
    assert IS.score(_pts(8), RULES)["решение"] == IS.BET
    assert IS.score(_pts(7), RULES)["решение"] == IS.WAIT
    assert IS.score(_pts(6), RULES)["решение"] == IS.WAIT
    assert IS.score(_pts(5), RULES)["решение"] == IS.ARCHIVE


def test_not_measured_is_not_counted():
    """«Не измерено» (None) — не балл: 7 зачтено + 3 не измерено = 7, а не 10 (П8)."""
    r = IS.score(_pts(7, n_none=3), RULES)
    assert r["балл"] == 7 and r["решение"] == IS.WAIT
    assert len(r["не_хватает"]) == 3 and r["до_ставки"] == 1
    assert all(p["почему"] for p in r["не_хватает"])          # причина словами — из листа


def test_blocker_forbids_bet_at_any_score():
    r = IS.score(_pts(10), RULES, blockers=["нет правила выхода"])
    assert r["балл"] == 10 and r["решение"] == IS.WAIT and r["брак"] == ["нет правила выхода"]


def test_truthy_non_bool_is_not_a_pass():
    """Зачтён только строгий True: строка «да» или 1 от модели балл не дают."""
    P = _pts(0)
    P[1] = {"зачтён": "да"}
    P[2] = {"зачтён": 1}
    assert IS.score(P, RULES)["балл"] == 0


def test_empty_points_archive():
    r = IS.score({}, RULES)
    assert r["балл"] == 0 and r["из"] == 10 and r["решение"] == IS.ARCHIVE


def test_asymmetry_long_short_and_bad_prices():
    assert IS.asymmetry(100, 130, 90, "вверх") == (3.0, 30.0, 10.0)
    assert IS.asymmetry(100, 80, 110, "вниз") == (2.0, 20.0, 10.0)
    assert IS.asymmetry(100, 90, 80, "вверх")[0] is None       # цель ниже входа — выигрыша нет
    assert IS.asymmetry(None, 1, 1)[0] is None and IS.asymmetry(0, 1, 1)[0] is None


def test_band():
    assert [IS.band(n, RULES) for n in (10, 8, 7, 6, 5, 0)] == [IS.BET, IS.BET, IS.WAIT, IS.WAIT, IS.ARCHIVE, IS.ARCHIVE]


# ── сектор ──
def _series(vals):
    return [(f"d{i:04d}", v) for i, v in enumerate(vals)]


def test_sector_short_history_not_measured():
    assert SR.measures(_series([100.0] * 150)) is None
    st, why = SR.status(None)
    assert st is None and why
    assert SR.against(None, "вверх") is None


def test_sector_correcting():
    v = [100.0] * 200 + [100 - i * 0.3 for i in range(1, 71)]       # три месяца вниз, ниже средней
    ms = SR.measures(_series(v), _series([100.0] * 270))
    st, _ = SR.status(ms)
    assert ms["над_средней"] < 0 and ms["за_3_месяца"] < 0 and st == SR.CORRECTING
    assert SR.against(st, "вверх") is True and SR.against(st, "вниз") is False


def test_sector_tailwind_vs_market():
    v = [100.0] * 200 + [100 + i * 0.1 for i in range(1, 71)]       # +7% выше средней, рынок стоит
    ms = SR.measures(_series(v), _series([100.0] * 270))
    st, why = SR.status(ms)
    assert st == SR.TAILWIND and SR.against(st, "вверх") is False and SR.against(st, "вниз") is True
    assert any("рынок" in w for w in why)


def test_sector_hot_at_the_high():
    v = [100.0] * 200 + [100 + i * 0.6 for i in range(1, 71)]       # +42%: далеко над средней, на максимуме
    st, _ = SR.status(SR.measures(_series(v), _series([100.0] * 270)))
    assert st == SR.HOT and SR.against(st, "вверх") is True


def test_sector_neutral_when_above_average_but_lagging_market():
    v = [100.0] * 200 + [100 + i * 0.05 for i in range(1, 71)]
    m = [100.0] * 200 + [100 + i * 0.5 for i in range(1, 71)]       # рынок вырос сильнее
    st, _ = SR.status(SR.measures(_series(v), _series(m)))
    assert st == SR.NEUTRAL and SR.against(st, "вверх") is False
