# -*- coding: utf-8 -*-
"""VQ-2 (владелец 07.11): «рынок ждёт X, мы ждём Y», существенность, устойчивость, жёсткая глубина цепочки —
проверяются КОДОМ в ops/evidence_check.py (брак → идея «для обсуждения», оговорка → код пишет в пуш)."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import evidence_check as EV   # noqa: E402

U = "https://example.com/x"


def _run(card):
    fails, labels = [], []
    out = {
        "mv": EV.check_market_view(card, fails, labels),
        "mat": EV.check_materiality(card, fails, labels),
    }
    EV.check_durability(card, fails, labels)
    EV.check_chain(card, fails, labels)
    return fails, labels, out


GOOD = {
    "market_view": {"kind": "magnitude", "metric": "EPS 2027, $",
                    "market": {"value": 2.10, "source": "EODHD Earnings::Trend +1y, 12 аналитиков", "as_of": "2026-11-07"},
                    "ours": {"value": 2.60},
                    "why_different": "консенсус не учитывает новые контракты по цене пола импорта с 4 декабря"},
    "materiality": {"effect_profit_per_year": 120, "years": 3, "net_income": 900, "market_cap": 2500},
    "durability": {"reason": "новый завод конкурента — не раньше 2028 (сроки строительства)", "url": U},
    "chain": [{"link": "пол цены ввоза", "quality": "confirmed", "url": U},
              {"link": "дороже импортная панель → заказы у американского производителя", "quality": "hypothesis"}],
}


def test_good_card_passes_with_one_hypothesis_label():
    fails, labels, out = _run(GOOD)
    assert fails == []
    assert out["mv"]["разрыв_отн"] == round((2.6 - 2.1) / 2.1, 4)
    assert out["mat"]["вердикт"] == "СУЩЕСТВЕННО"
    assert any("гипотеза" in x for x in labels)


def test_missing_market_view_is_brak():
    card = dict(GOOD)
    card.pop("market_view")
    fails, _l, _o = _run(card)
    assert any("рынок уже ждёт" in f for f in fails)


def test_same_as_market_and_no_source_and_no_numbers():
    c = dict(GOOD, market_view=dict(GOOD["market_view"], ours={"value": 2.15}))
    assert any("почти совпадает" in f for f in _run(c)[0])
    c = dict(GOOD, market_view=dict(GOOD["market_view"], market={"value": 2.1}))
    assert any("нет источника" in f for f in _run(c)[0])
    c = dict(GOOD, market_view=dict(GOOD["market_view"], ours={"value": "больше"}))
    assert any("нет чисел" in f for f in _run(c)[0])


def test_timing_and_flow_kinds():
    t = dict(GOOD, market_view={"kind": "timing", "market": {"date": "2027-03-01", "source": "календарь"},
                                "ours": {"date": "2026-12-04"}, "why_different": "x" * 50})
    assert _run(t)[0] == []
    t2 = dict(t, market_view=dict(t["market_view"], ours={"date": "2027-03-01"}))
    assert any("двух разных дат" in f for f in _run(t2)[0])
    fl = dict(GOOD, market_view={"kind": "flow", "market": {"source": "отчёт фонда о ликвидации"},
                                 "why_different": "фонд обязан продать до конца квартала независимо от цены"})
    f, lab, _o = _run(fl)
    assert f == [] and any("вынужденных продажах" in x for x in lab)


def test_implied_growth_recomputed_by_code():
    imp = {"market_cap": 160000, "base_earnings": 1700, "r": 0.09, "g": 0.025, "years": 10}
    c = dict(GOOD, market_view=dict(GOOD["market_view"], metric="рост прибыли в год", implied=imp,
                                    market={"value": 0.15, "source": "обратный DCF"}, ours={"value": 0.35}))
    f, _l, out = _run(c)
    assert any("расходится с пересчётом" in x for x in f)          # на самом деле зашито ~26%
    assert out["mv"]["зашито_в_цену"]["ok"]


def test_materiality_noise_is_brak_and_missing_is_label():
    c = dict(GOOD, materiality={"effect_profit_per_year": 50, "net_income": 100000})
    assert any("шум" in f for f in _run(c)[0])
    c = dict(GOOD)
    c.pop("materiality")
    f, lab, _o = _run(c)
    assert f == [] and any("не посчитана" in x for x in lab)


def test_chain_hard_stop():
    c = dict(GOOD, chain=GOOD["chain"] * 2)
    assert any("жёсткий предел" in f for f in _run(c)[0])
    c = dict(GOOD, chain=[{"link": "a", "quality": "hypothesis"}, {"link": "b", "quality": "hypothesis"}])
    assert any("гипотезе поверх гипотезы" in f for f in _run(c)[0])
    c = dict(GOOD, chain=[{"link": "a", "quality": "confirmed"}])           # «подтверждено» без источника — гипотеза
    assert any("гипотеза" in x for x in _run(c)[1])
    c = dict(GOOD)
    c.pop("chain")
    assert any("не показаны звенья" in f for f in _run(c)[0])


def test_durability_label_and_in_full_check():
    c = dict(GOOD)
    c.pop("durability")
    assert any("Устойчивость" in x for x in _run(c)[1])
    full = EV.check(c)
    assert "market_view" in full and any("Устойчивость" in x for x in full["labels"])


def test_agent_tools_compute_by_code(tmp_path, monkeypatch):
    import json as _json
    import bot_agent as A
    t = A.Tools()
    out = t.implied_expectations(market_cap=160000, base_earnings=1700, our_growth=0.35)
    rec = t.results["r1"]["data"]
    assert rec["ok"] and 0.2 < rec["рост_в_год"] < 0.35 and "БОЛЬШЕ" in rec["сравнение"]["вывод"]
    assert "обратный DCF" in out
    assert t.implied_expectations(market_cap=1000).startswith("ошибка")
    t.materiality_check(effect_profit_per_year=50, net_income=100000)
    assert t.results["r2"]["data"]["вердикт"] == "ШУМ"
    log = tmp_path / "reports" / "ideas"
    log.mkdir(parents=True)
    import datetime as _dt
    today = _dt.date.today().isoformat()
    (log / "hunt_log.jsonl").write_text(_json.dumps({"дата": today, "инструмент": "CF", "вердикт": "убита",
                                                     "причина": "в цене", "пересмотреть_если": "газ +30%"},
                                                    ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(A, "ROOT", tmp_path)
    assert '"веток": 1' in t.checked_branches(7)


def test_honest_zero_shows_checked_branches(tmp_path, monkeypatch):
    import json as _json
    import bot_state as S
    import bot as BOT
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "s.json")
    d = tmp_path / "ideas"
    d.mkdir()
    (d / "idea_2026-11-09_none.json").write_text(_json.dumps({
        "checked": 4, "reasons": ["у всех кандидатов рост уже заложен в цену"],
        "branches": [{"what": "Freeport-McMoRan (медь)", "why_not": "рынок уже ждёт рост прибыли на 25% в год",
                      "return_if": "запасы меди на бирже упадут ниже 100 тыс. т"}]}, ensure_ascii=False), encoding="utf-8")

    class TG:
        def send_message(self, *a, **k):
            return 1
    b = BOT.Bot(TG(), "555", state=S._default_state())
    monkeypatch.setattr(b, "_ideas_dir", lambda: d)
    fn = [n for n in dir(b) if "zero" in n.lower() and callable(getattr(b, n))]
    text = None
    for n in fn:
        try:
            text = getattr(b, n)("2026-11-09")
        except TypeError:
            continue
        if isinstance(text, str):
            break
    assert isinstance(text, str) and "Что ещё смотрел" in text and "Вернусь, если" in text
