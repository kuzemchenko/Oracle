# -*- coding: utf-8 -*-
"""Память аналогий (ops/analogs.py): похожие исторические каскады — детерминированно."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ops import analogs as AN   # noqa: E402

EPS = [
    {"id": "1987_tanker_war", "trigger_date": "1987-07-22", "title_ru": "Танкерная война: конвои в Персидском заливе",
     "what_ru": "Иран атакует танкеры, США сопровождают кувейтские суда", "mechanism_id": "chokepoint_disruption",
     "shock_type": "war", "resources": ["oil", "tanker_capacity"], "region": "Персидский залив",
     "links": [{"order": 0, "role_ru": "нефть", "role_tag": "crude_oil", "then_symbol": "FRED:DCOILWTICO",
                "today_symbol": "USO", "direction": "up", "prices": {"есть": True, "ход_5": 0.05, "ход_20": 0.02,
                                                                     "ход_60": -0.1}},
               {"order": 1, "role_ru": "страховщик военных рисков", "role_tag": "war_risk_insurer",
                "then_symbol": "нет", "today_symbol": "AXS", "direction": "up", "non_obvious": True,
                "prices": {"есть": False, "причина": "тикер того времени не указан"}}],
     "counter_case_ru": "2019: атаки без перекрытия — откат за 2 недели", "analog_rule_ru": "когда…",
     "sources": [{"url": "https://en.wikipedia.org/wiki/Tanker_war"}]},
    {"id": "2012_us_drought", "trigger_date": "2012-06-20", "title_ru": "Засуха в США", "what_ru": "кукуруза",
     "mechanism_id": "breadbasket_drought", "shock_type": "weather", "resources": ["corn"], "region": "США",
     "links": [], "counter_case_ru": "", "analog_rule_ru": ""},
]


def test_score_and_ranking_are_deterministic():
    a = AN.find("Ормуз танкеры Иран Персидский залив", mechanism="chokepoint_disruption", episodes=EPS)
    b = AN.find("Ормуз танкеры Иран Персидский залив", mechanism="chokepoint_disruption", episodes=EPS)
    assert a == b and a[0][1]["id"] == "1987_tanker_war" and len(a) == 1
    assert AN.score(EPS[0], mechanism="chokepoint_disruption") == 4
    assert AN.score(EPS[0], shock="war", resources=["oil", "lng"]) == 2 + 1.5
    assert AN.find("совсем другое слово", episodes=EPS) == []


def test_render_shows_code_moves_roles_and_counter_case():
    txt = AN.render(AN.find("танкеры", episodes=EPS),
                    {("USO", "AXS", 1): {"эпизоды": ["1987_tanker_war"], "вердикт": "намёк", "шоки_источника": "вверх",
                                          "счёт": {"пошло": 9, "крупных_ходов": 14, "ожидание_по_знаку": 6.5}}})
    assert "код: 5д +5%, 20д +2%, 60д -10%" in txt                       # exp(x)−1, округление
    assert AN._move(-1.25) == "-71%" and AN._move(0.6931) == "+100%"
    assert "сегодня AXS" in txt and "(неочевидно)" in txt
    assert "история пары USO→AXS (шоки вверх): намёк — из 14 крупных ходов в сторону механизма 9 при ожидании по знаку 6.5" in txt
    assert "провальный аналог: 2019" in txt
    assert "Похожих эпизодов" in AN.render([])


def test_history_prices_norm_keeps_exchange_suffixes():
    from ops.history_prices import norm
    assert norm("BRK.B") == "BRK-B" and norm("fro.us") == "FRO"
    assert [norm(x) for x in ("0293.HK", "9501.T", "RWE.DE", "BP.L", "CJT.TO", "AALI.JK")] == \
        ["0293.HK", "9501.T", "RWE.DE", "BP.L", "CJT.TO", "AALI.JK"]
    assert norm("CL=F") == "CL=F" and norm("^GSPC") == "^GSPC" and norm("FRED:dexchus") == "FRED:DEXCHUS"
    assert norm("нет") is None
