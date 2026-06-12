# -*- coding: utf-8 -*-
"""Тесты портфельного менеджера §4: фикс 0.5% до gate, карта корреляций по макро-драйверам."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import portfolio as PF  # noqa: E402


def test_macro_driver_mapping():
    assert PF.macro_driver("BNO.US") == "oil"
    assert PF.macro_driver("USO.US") == "oil"
    assert PF.macro_driver("CPER.US") == "copper"
    assert PF.macro_driver("COPX.US") == "copper"
    assert PF.macro_driver("UNKNOWN.US") == "unmapped"  # П8: не выдумываем связь


def test_correlation_map_same_driver_one_bet():
    # лонг BNO + лонг USO = ОДНА нефтяная ставка (§4), не две
    ideas = [{"актив": "BNO.US", "направление": "лонг", "amount_usd": 500},
             {"актив": "USO.US", "направление": "лонг", "amount_usd": 500}]
    cm = PF.correlation_map(ideas)
    assert cm["n_независимых_ставок"] == 1
    assert any(w["тип"] == "одна_ставка_много_тикеров" for w in cm["предупреждения"])


def test_correlation_map_opposite_signs_distinct():
    # лонг и шорт одного драйвера — разные подписанные экспозиции
    ideas = [{"актив": "BNO.US", "направление": "лонг", "amount_usd": 500},
             {"актив": "USO.US", "направление": "шорт", "amount_usd": 500}]
    cm = PF.correlation_map(ideas)
    assert cm["n_независимых_ставок"] == 2


def test_correlation_map_hidden_commodity_concentration():
    # лонг нефти + лонг меди → разные драйверы, но общая сырьевая бета → предупреждение
    ideas = [{"актив": "BNO.US", "направление": "лонг", "amount_usd": 500},
             {"актив": "CPER.US", "направление": "лонг", "amount_usd": 500}]
    cm = PF.correlation_map(ideas)
    assert any(w["тип"] == "скрытая_концентрация" for w in cm["предупреждения"])


def test_build_portfolio_fixed_microsize_before_gate():
    # §11: до gate калибровки — ФИКС 0.5% капитала/идея, Келли не применяется
    ideas = [{"актив": "BNO.US", "направление": "лонг", "вероятность": 0.8, "b": 2.0}]
    port = PF.build_portfolio(ideas, capital=100000, gate_passed=False)
    pos = port["позиции"][0]
    assert pos["sizing_method"] == "fixed_microsize"
    assert abs(pos["amount_usd"] - 500.0) < 1e-6  # 0.5% от 100k
    assert "фикс 0.5%" in port["режим_размера"]
    # лимит на идею пройден ($500 = потолок)
    assert all(c["allowed"] for c in port["проверка_лимитов"]["на_идею"])


def test_build_portfolio_kelly_after_gate():
    ideas = [{"актив": "BNO.US", "направление": "лонг", "вероятность": 0.8, "b": 2.0}]
    port = PF.build_portfolio(ideas, capital=100000, gate_passed=True, calibration_proven=1.0,
                              kelly_multiplier=0.5)
    assert port["позиции"][0]["sizing_method"] == "fractional_kelly"


def test_build_portfolio_monthly_limit_check():
    # три идеи по $500 = $1500 < $3000 месячного потолка → ok
    ideas = [{"актив": s, "направление": "лонг", "вероятность": 0.7, "b": 1.0}
             for s in ("BNO.US", "CPER.US", "SPY.US")]
    port = PF.build_portfolio(ideas, capital=100000, gate_passed=False)
    assert port["проверка_лимитов"]["месячный"]["allowed"] is True
    assert port["суммарный_риск_usd"] == 1500.0
