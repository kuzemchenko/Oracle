# -*- coding: utf-8 -*-
import pytest

from mathlib import bet_sheet as BS


def test_brnt_like_threshold_and_pnl():
    r = BS.sheet(96.24, [{"name": "эскалация", "price": 120}, {"name": "без изменений", "price": 96},
                         {"name": "мир", "price": 80}], qty=2, fees_usd=4)
    assert r["ok"] and r["позиция"]["вложено_usd"] == pytest.approx(192.48)
    pnl = {s["name"]: s["pnl_usd"] for s in r["сценарии"]}
    assert pnl["эскалация"] == pytest.approx((120 - 96.24) * 2 - 4)
    assert pnl["мир"] == pytest.approx((80 - 96.24) * 2 - 4)
    p = -pnl["мир"] / (pnl["эскалация"] - pnl["мир"])
    assert r["порог_шанса"] == pytest.approx(round(p, 4)) and "эскалация" in r["порог_словами"]
    assert "ожидание_usd" not in r                       # шансы не названы — ожидание не выдумываем


def test_expected_value_only_when_probabilities_sum_to_one():
    sc = [{"name": "да", "price": 12, "probability": 0.4}, {"name": "нет", "price": 9, "probability": 0.6}]
    r = BS.sheet(10, sc, size_usd=1000)
    assert r["ожидание_usd"] == pytest.approx(0.4 * 200 + 0.6 * -100)
    bad = BS.sheet(10, [dict(sc[0], probability=0.5), dict(sc[1], probability=0.6)], size_usd=1000)
    assert "ожидание_usd" not in bad and "в сумме" in bad["причина_без_ожидания"]


def test_short_and_refusals():
    r = BS.sheet(50, [{"name": "падение", "price": 40}, {"name": "рост", "price": 55}], qty=10, direction="short")
    assert {s["name"]: s["pnl_usd"] for s in r["сценарии"]} == {"падение": 100.0, "рост": -50.0}
    assert not BS.sheet(None, [{"price": 1}, {"price": 2}], qty=1)["ok"]
    assert not BS.sheet(10, [{"price": 1}], qty=1)["ok"]
    assert not BS.sheet(10, [{"price": 1}, {"price": 2}])["ok"]
    assert "нет выигрыша" in BS.sheet(10, [{"price": 9}, {"price": 8}], qty=1)["порог_словами"]
