# -*- coding: utf-8 -*-
"""Тесты П2б (§R4.4, подпись 05.07): продуктовый ранг показа «неочевидность × свежесть ×
доказуемость» — детерминизм, честные прайоры, пометка поздних фаз, граница слоя Б."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import product_rank as PR       # noqa: E402


def _idea(order=3, chok=False, свежесть=None, фаза=None, r2=None, att_status="ok"):
    внимание = ({"статус": att_status, "свежесть": свежесть, "фаза": фаза}
                if (свежесть is not None or фаза) else {})
    return {"актив": "X.US",
            "аргумент": {"порядок_узла": order, "чокпоинт": chok, "надёжность_r2": r2},
            "внимание": внимание}


def test_rank_deterministic_and_bounded():
    r = PR.rank_idea(_idea(order=4, свежесть=0.8, r2=0.6))
    assert r == PR.rank_idea(_idea(order=4, свежесть=0.8, r2=0.6))     # детерминизм
    assert r["балл"] == round(1.0 * 0.8 * 0.6, 4)
    assert all(0.0 <= v <= 1.0 for v in r["компоненты"].values())
    assert r["не_измерено"] == []


def test_priors_declared_not_invented():
    # П8/§R0#5: неизмеренная компонента — нейтральный прайор 0.5 С ДЕКЛАРАЦИЕЙ, не выдумка
    r = PR.rank_idea({"актив": "X.US"})
    assert r["компоненты"] == {"неочевидность": 0.5, "свежесть": 0.5, "доказуемость": 0.5}
    assert len(r["не_измерено"]) == 3
    # None-свежесть НЕ равна 1.0 (нет награды за отсутствие данных)
    измеренная = PR.rank_idea(_idea(order=2, свежесть=1.0, r2=0.5))
    неизмеренная = PR.rank_idea(_idea(order=2, r2=0.5))
    assert неизмеренная["компоненты"]["свежесть"] < измеренная["компоненты"]["свежесть"]


def test_late_phase_always_labeled():
    # §R5: ПОЗДНО/ЛОВУШКА/ОТЫГРАНО не в топе без явной пометки
    for фаза in PR.LATE_PHASES:
        assert PR.rank_idea(_idea(свежесть=0.1, фаза=фаза))["поздняя_фаза"] == фаза
    assert PR.rank_idea(_idea(свежесть=0.9, фаза="РАНО"))["поздняя_фаза"] is None


def test_chokepoint_and_depth_raise_nonobviousness():
    shallow = PR.rank_idea(_idea(order=2))["компоненты"]["неочевидность"]
    deep = PR.rank_idea(_idea(order=4))["компоненты"]["неочевидность"]
    chok = PR.rank_idea(_idea(order=2, chok=True))["компоненты"]["неочевидность"]
    assert shallow < deep and shallow < chok <= 1.0


def test_annotate_and_sort_stable_and_desc():
    hot = _idea(order=2, свежесть=0.1, r2=0.9)          # перегретая мелкая
    fresh = _idea(order=4, свежесть=0.9, r2=0.9)        # глубокая свежая
    tie_a, tie_b = {"актив": "A.US"}, {"актив": "B.US"}  # равные прайоры → стабильность
    out = PR.annotate_and_sort([hot, tie_a, tie_b, fresh])
    assert out[0] is fresh                               # лучший балл наверху
    assert all("продуктовый_ранг" in i for i in out)
    ia, ib = out.index(tie_a), out.index(tie_b)
    assert ia < ib                                       # при равенстве — исходный порядок


def test_session_uses_rank_but_combat_selection_untouched():
    # граница слоя Б: сессия показывает по рангу, а боевой протокол (треки/суд) не мутирует ранг
    from orchestrator import partner_session as PS
    protocol = {
        "run_id": "t", "ts": "2026-07-05T09:00:00+00:00",
        "граф_отбор": {
            "money_трек": [{"актив": "HOT.US", "направление": "лонг", "порядок": 2,
                            "надёжность_r2": 0.9,
                            "внимание": {"статус": "ok", "свежесть": 0.05, "фаза": "ЛОВУШКА"}}],
            "провизорный_трек": [{"актив": "DEEP.US", "направление": "лонг", "порядок": 4,
                                  "провизорный": True, "надёжность_r2": 0.8,
                                  "внимание": {"статус": "ok", "свежесть": 0.9, "фаза": "РАНО"}}],
            "суд_money": {},
        },
        "картограф_идеи": [],
    }
    s = PS.build_session(protocol, asof="2026-07-05T12:00:00+00:00")
    ideas = s["идеи"]
    assert [i["актив"] for i in ideas] == ["DEEP.US", "HOT.US"]     # ранг перевесил порядок треков
    assert ideas[1]["продуктовый_ранг"]["поздняя_фаза"] == "ЛОВУШКА"  # пометка не потерялась
    # протокол-источник не тронут: боевые треки в исходном порядке (пере-ранжирован только показ)
    assert protocol["граф_отбор"]["money_трек"][0]["актив"] == "HOT.US"
    assert "П2б" in s["spec_ref"]
