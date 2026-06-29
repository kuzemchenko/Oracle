# -*- coding: utf-8 -*-
"""Точечные тесты event-first доставки (агент B, F1): баги #10/#12/#14.

Закрепляют:
  • #10 — ideas_from_protocol по ef-протоколу даёт НЕпустой список единого контракта (раньше читал
    только легаси этап6_синтез.отчёты → пусто для ef_*); money_ideas_from_protocol fail-closed;
  • #12 — money-полка дайджеста = число переживших слепой суд («УСТОЯЛА»), а НЕ сырой трек до суда;
  • #14 — истории картографа в дайджесте не режутся до 3 (показываем все/первые CARTO_SHOW + хвост).
Сети/БД-контеншна нет: фикстуры — обычные dict-протоколы.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))

import bot_reports as R   # noqa: E402


def _node(asset, *, prob=0.4, edge=0.05, prov=False, event=None):
    return {"актив": asset, "score": 0.1, "edge": edge,
            "направление": "лонг" if edge >= 0 else "шорт",
            "вероятность": prob, "надёжность_r2": 0.3, "порядок": 2,
            "якорь": "BNO.US", "провизорный": prov, "событие": event}


_DEFAULT = object()


def _ef_proto(*, n_carto=7, money_verdicts=_DEFAULT):
    """Боевой (auto) ef-протокол: 3 money-узла + суд по ним + n_carto историй картографа."""
    money = [_node("VLO.US", event="новость про НПЗ"),
             _node("FRO.US", event="новость про танкеры"),
             _node("LNG.US", event="новость про СПГ")]
    verdicts = {
        "VLO.US": {"исход": "УСТОЯЛА", "балл": 3.4, "порог": 3.0},
        "FRO.US": {"исход": "РАЗБИТА", "балл": 2.1, "порог": 3.0},
        "LNG.US": {"исход": "РАЗБИТА", "балл": 1.8, "порог": 3.0},
    } if money_verdicts is _DEFAULT else money_verdicts
    карто = [{"актив": f"C{i}.US", "все_инструменты": [f"C{i}.US"],
              "событие": f"СОБЫТИЕ-{i}", "тектонический_потенциал": 0.5,
              "узлы_каскада": [{"порядок": 2, "узел": f"звено {i}", "тикеры": [f"C{i}.US"]}]}
             for i in range(n_carto)]
    return {
        "run_id": "ef_20260629T000000Z", "ts": "2026-06-29T00:00:00+00:00", "mode": "auto",
        "картограф_идеи": карто,
        "каскады_в_компании": [],
        "граф_отбор": {
            "узлов": 12, "ворота_прошли": 5,
            "треки": {"money": 3, "провизорный": 2, "дайджест": 0},
            "запечатано": {"money": 1, "провизорный": 4},
            "суд_money": verdicts,
            "топ_k": money + [_node("EEM.US", prov=True, event="развив. рынки")],
            "money_трек": money,
            "провизорный_трек": [_node("EEM.US", prov=True)],
        },
    }


# ── #10: ideas_from_protocol по ef-протоколу НЕ пуст ─────────────────────────────────
def test_ideas_from_protocol_ef_nonempty():
    proto = _ef_proto()
    ideas = R.ideas_from_protocol(proto)
    assert ideas, "ef-протокол обязан давать непустой поток идей (#10)"
    assets = {i.get("актив") for i in ideas}
    assert "VLO.US" in assets and "C0.US" in assets   # граф-топ ∪ картограф
    # форма контракта: поля, нужные боту/чату/§8-промоушену
    card = next(i for i in ideas if i["актив"] == "VLO.US")
    assert card["направление"] == "лонг"
    assert card["позиция"]["вероятность"] == 0.4
    assert card["отчёт"]["поля"]   # есть §8-поля → idea_brief/format_report работают
    br = R.idea_brief(card)
    assert br["актив"] == "VLO.US" and br["каскад"]   # суть восстановима


def test_legacy_protocol_still_returns_stage6():
    legacy = {"run_id": "funnel_x", "этап6_синтез": {"отчёты": [{"актив": "BNO.US"}]}}
    assert R.ideas_from_protocol(legacy) == [{"актив": "BNO.US"}]


def test_money_ideas_failclosed_only_survivors():
    # только VLO.US «УСТОЯЛА» → ровно одна money-идея под §11-промоушен (агент C, #11)
    money = R.money_ideas_from_protocol(_ef_proto())
    assert [m["актив"] for m in money] == ["VLO.US"]
    # суд не гонялся → fail-closed: ни одной money-идеи
    none_court = _ef_proto(money_verdicts={})
    assert R.money_ideas_from_protocol(none_court) == []


# ── #12: money-полка дайджеста считается ПОСЛЕ суда ──────────────────────────────────
def test_digest_money_shelf_counts_after_court():
    text = R.format_research_digest(_ef_proto())
    # пережила суд ровно одна (VLO.US «УСТОЯЛА»), а НЕ сырые 3 трека до суда
    assert "💰 1 " in text
    assert "💰 3 " not in text
    # две демотированные money-идеи уходят в полку гипотез: 2(провиз.) + 2(демот.) = 4
    assert "🧪 4 " in text


def test_money_after_court_helper():
    survived, demoted = R._money_after_court(_ef_proto()["граф_отбор"])
    assert (survived, demoted) == (1, 2)
    # процедурное вето демотирует даже «УСТОЯЛА» (§6)
    veto = _ef_proto(money_verdicts={"VLO.US": {"исход": "УСТОЯЛА", "процедурное_вето": True},
                                     "FRO.US": {"исход": "РАЗБИТА"}, "LNG.US": {"исход": "РАЗБИТА"}})
    assert R._money_after_court(veto["граф_отбор"]) == (0, 3)


# ── #14: истории картографа не режутся до 3 ──────────────────────────────────────────
def test_digest_cartographer_not_capped_at_three():
    text = R.format_research_digest(_ef_proto(n_carto=7))
    # 4-я и 5-я истории присутствуют (раньше срез [:3] их хоронил)
    assert "СОБЫТИЕ-3" in text and "СОБЫТИЕ-4" in text
    # сверх CARTO_SHOW=5 хвост уходит в /progress, а не теряется молча
    assert "СОБЫТИЕ-5" not in text
    assert "ещё 2 таких историй" in text


def test_digest_shows_all_when_few():
    text = R.format_research_digest(_ef_proto(n_carto=4))
    for i in range(4):
        assert f"СОБЫТИЕ-{i}" in text
    assert "таких историй" not in text   # хвоста нет — историй мало, показаны все
