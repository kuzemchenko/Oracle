# -*- coding: utf-8 -*-
"""V0 «Идея дня» (карта v3, 31.10.2026; формат владельца 15.07): шаблон + блокирующий линт подачи.
Детерминированно, без LLM/сети; база трека подаётся явно (журнал не читаем)."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import bot_reports as R          # noqa: E402
import presentation_lint as PL   # noqa: E402

BASE = {"hit": 0.483, "n": 1932}


def _entry(asset, order, frac, edge=0.071, chain="c1", node="узел цепочки", direction="лонг"):
    return {"актив": asset, "score": 0.3, "edge": edge, "направление": direction, "ярусы": ["C"],
            "лаг_дней": 0, "горизонт_дней": 5, "вероятность": 0.467, "надёжность_r2": 0.02,
            "цепочка": chain, "порядок": order, "чокпоинт": False, "отыгранность_узла": frac,
            "узлы_каскада": [{"порядок": order, "узел": node, "тикеры": [asset]}],
            "провизорный": True, "инверсия": False, "внимание": None,
            "событие": "Технологические акции США теряют $2 трлн капитализации в октябре",
            "якорь": "VXX.US", "источник_карты": "картограф"}


def _proto(top, court=None):
    court = court if court is not None else {
        "NVDA.US": {"оценка": 2.8, "band": "средняя",
                    "критерии": {"mechanism_real": 1.0, "non_obviousness": 4.0},
                    "кто_против": "Продают инвесторы, уходящие в защитные активы (RSI 35, цена 212.08).",
                    "почему_возможность": "Перепроданность и неотыгранный ход +7.1%."}}
    return {"run_id": "ef_20261031T090001Z", "ts": "2026-10-31T09:00:01+00:00",
            "граф_отбор": {"топ_k": top, "провизорный_трек": top, "money_трек": [],
                           "разбор_идей": court, "суд_money": {}},
            "поток_идей": {"всего": len(top)}}


def test_idea_of_day_passes_lint_and_has_required_lines():
    top = [_entry("NVDA.US", 4, 0.0), _entry("FRO.US", 2, 0.1, edge=0.02), _entry("DAL.US", 3, 0.9, edge=-0.03)]
    text, meta, viol = R.idea_of_day_from_protocol(_proto(top), base_rate=BASE)
    assert viol == [] and text
    assert meta["idx"]["1"] == "NVDA.US"
    assert text.startswith("Идея дня — 31 октября. Nvidia (NVDA.US): ставка на рост.")
    assert "Потенциал." in text and "шанс около 48%" in text and "матожидание около -0.2%" in text
    assert "Кто продаёт нам." in text and "Суд." in text and "Счёт честности." in text
    assert "Сверю к 5 ноября." in text
    assert "Сила" not in text and "тех.id" not in text


def test_chain_marks_each_link_and_nearer_unplayed_is_admitted():
    top = [_entry("NVDA.US", 4, 0.0), _entry("FRO.US", 2, 0.1, edge=0.02), _entry("DAL.US", 3, 0.9, edge=-0.03)]
    text = R.format_idea_of_day(_proto(top), base_rate=BASE)
    assert "— второй шаг:" in text and "Frontline (FRO.US) — ещё не отыграно" in text
    assert "третий шаг:" in text and "рынок уже отыграл" in text
    assert "четвёртый шаг:" in text and "моё звено" in text
    assert "ближе к событию есть неотыгранные звенья (Frontline (FRO.US))" in text


def test_numbers_cap_counts_outside_quotes_and_dates():
    text = R.format_idea_of_day(_proto([_entry("NVDA.US", 4, 0.0)]), base_rate=BASE)
    nums = PL.count_numbers(text)
    assert len(nums) <= PL.IDEA_DAY_MAX_NUMBERS, nums
    # числа в цитате суда («RSI 35, цена 212.08») и в дате «5 ноября» не считаются
    assert not any("212" in n or "35" == n.strip("%") for n in nums)


def test_lint_blocks_bare_ticker_tech_id_and_too_many_numbers():
    bad = ("Идея дня. NVDA.US: ставка на рост.\nЧто это значит для тебя. x\n"
           "Ход 1%, 2%, 3%, 4%, 5%.\nЧто делать. y · тех.id ef_20261031T090001Z")
    viol = PL.lint_idea_of_day(bad)
    joined = " | ".join(viol)
    assert "тикеры без имени: NVDA.US" in joined
    assert "тех.id" in joined
    assert "чисел вне цитат" in joined and "> 4" in joined


def test_unknown_company_is_marked_honestly_not_invented():
    top = [_entry("ZZZQ.US", 2, 0.0)]
    text, meta, viol = R.idea_of_day_from_protocol(_proto(top, court={}), base_rate=BASE)
    assert viol == [] and f"ZZZQ.US — {PL.UNKNOWN_NAME_MARK}" in text
    assert "Суд. Идею сегодня не судили" in text and "Суд не отвечал" in text


def test_honest_zero_is_one_short_block_and_passes_lint():
    text, meta, viol = R.idea_of_day_from_protocol(_proto([], court={}), base_rate=BASE)
    assert viol == [] and text.count("\n") <= 4 and "Сегодня стоящего нет" in text
    assert meta["idx"] == {}


def test_no_base_rate_is_honest():
    text = R.format_idea_of_day(_proto([_entry("NVDA.US", 4, 0.0)]), base_rate={"hit": None, "n": 0})
    assert "шанс не измерен" in text


def test_top3_has_no_strength_score_and_shows_base(monkeypatch):
    monkeypatch.setattr(R, "track_base_rate", lambda *a, **k: BASE)
    text = R.format_top3(_proto([_entry("NVDA.US", 4, 0.0)]))
    assert "/5" not in text and "Сила" not in text
    assert "сбывались примерно в 48% случаев" in text and "Суд оценил как средняя" in text


def test_track_base_rate_reads_outcomes_kind(tmp_path):
    p = tmp_path / "o.jsonl"
    p.write_text('{"kind":"cascade_provisional","outcome":1}\n{"kind":"cascade_provisional","outcome":0}\n'
                 '{"kind":"calibration","outcome":1}\nмусор\n', encoding="utf-8")
    r = R.track_base_rate("cascade_provisional", path=p)
    assert r == {"hit": 0.5, "n": 2}
    assert R.track_base_rate("edge_forward", path=p) == {"hit": None, "n": 0}
