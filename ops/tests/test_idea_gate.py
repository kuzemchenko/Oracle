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
    # перепроверка №2: проценты — из цен (код), заявленные обязаны сходиться (±2 п.п.)
    "trigger_date": "2026-10-01", "trigger_close": 41.21875,      # 39.57 / 41.21875 − 1 = −4%
    "target_price": 51.441, "stop_price": 35.613,                 # +30% / −10% от 39.57
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
        (_c(move_since_trigger_pct=55.0, trigger_close=round(39.57 / 1.55, 6)), "уже отыграно"),
        (_c(upside_pct=12, target_price=round(39.57 * 1.12, 6)), "асимметрия"),
        (_c(upside_pct=25), "не сходится с ценами"),                 # заявлено ≠ цены карточки
        (_c(move_since_trigger_pct=-15.0), "не сходится с ценами"),
        (_c(stop_price=40.0), "за стопом"),
        (_c(entry_limit=42.0), "асимметрия"),                        # от худшего входа 1.48 < 1.5
        (_c(trigger_date=None), "нет поля «trigger_date»"),
        (_c(target_price=None), "нет поля «target_price»"),
        (_c(facts=CARD["facts"] + [{"text": "x", "url": "https://a.org/b", "date": "2026-11-05"}]),
         "позже сегодняшнего"),
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
    assert (sc["идей"], sc["исходов"], sc["попаданий"]) == (1, 1, 1)
    lines = HS.footer(rec, None, sc)
    joined = " ".join(lines)
    # перепроверка №2: прогноз — вопрос, который сверю, а не утверждение «закроется»
    assert "закроется ли MP выше 45 к 15 декабря" in joined and "сбылось 1 из 1" in joined
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
    down = dict(direction="вниз", target_price=round(39.57 * 0.8, 6), stop_price=round(39.57 * 1.08, 6),
                upside_pct=20, downside_pct=8,
                spec9={**CARD["spec9"], "direction": "below", "threshold": 35.0})
    down_played = _c(move_since_trigger_pct=-55.0, trigger_close=round(39.57 / 0.45, 6), **down)
    r = G.check(down_played, today=TODAY)
    assert any("уже отыграно" in f for f in r["fails"]), r["fails"]
    # та же идея, бумага пока −4% — не отыграно
    down_fresh = _c(move_since_trigger_pct=-4.0, **down)
    assert G.check(down_fresh, today=TODAY)["pass"], G.check(down_fresh, today=TODAY)["fails"]
    r2 = G.check(_c(upside_pct=90, target_price=round(39.57 * 1.9, 6)), today=TODAY)
    assert any("больше прецедентного хода" in f for f in r2["fails"])
    r3 = G.check(_c(spec9={**CARD["spec9"], "threshold": 60.0}), today=TODAY)
    assert any("больше потенциала до цели" in f for f in r3["fails"])


def test_server_check_uses_server_price_and_rejects_undated():
    ok, fails, c2 = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                    last_close_fn=lambda a: (46.0, "2026-10-31"))
    assert not ok and any("уже-истинной" in f for f in fails)   # серверная цена выше порога 45
    assert c2["spec9"]["last_close"] == 46.0
    ok2, fails2, _ = HS.server_check(CARD, idea_file="idea_MP.push.txt", today=TODAY)
    assert not ok2 and "нет даты" in fails2[0]
    ok3, f3, _ = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                 last_close_fn=lambda a: (None, None))
    assert not ok3 and "нет серверной цены" in f3[0]             # цена со слов LLM — не основание
    assert HS.is_transient(f3)                                   # но это сбой данных, не брак идеи


def _prices(last=39.57, last_date="2026-10-30", trig=41.21875, n=61):
    closes = [39.57 * (1.03 if i % 2 else 0.97) for i in range(n - 1)] + [last]
    return lambda a, td=None: {"last_close": last, "last_date": last_date, "trigger_close": trig,
                               "trigger_date": td, "closes": closes}


def test_server_recomputes_percentages_from_server_prices():
    """Перепроверка №2: сервер считает ход/потенциал/риск по СВОИМ котировкам, а не по числам
    LLM; цена в день повода в карточке сверяется с котировками (допуск 3%); база шума — кодом."""
    ok, fails, c2 = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                    prices_fn=_prices())
    assert ok, fails
    assert c2["upside_pct"] == 30.0 and c2["downside_pct"] == 10.0 and c2["move_since_trigger_pct"] == -4.0
    bp = c2["spec9"]["base_probability"]
    assert bp is not None and 0 < bp < 0.5
    # цена ушла к 50 → потенциал до цели 51.441 съёжился, асимметрия от цены провалена — кодом
    ok2, f2, c3 = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                  prices_fn=_prices(last=44.0))
    assert not ok2 and any("асимметрия" in f for f in f2) and c3["upside_pct"] < 30
    # автор ошибся в цене в день повода больше чем на 3% — брак (не повтор)
    ok3, f3, _ = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                 prices_fn=_prices(trig=45.0))
    assert not ok3 and "автор ошибся" in f3[0] and not HS.is_transient(f3)
    # устаревшие серверные котировки — повтор, не брак
    ok4, f4, _ = HS.server_check(CARD, idea_file=f"idea_{TODAY}_MP.push.txt", today=TODAY,
                                 prices_fn=_prices(last_date="2026-10-01"))
    assert not ok4 and HS.is_transient(f4)


def test_server_horizon_measured_from_hunt_date():
    """Перепроверка №2: горизонт 3–13 недель меряется от даты охоты — идея на нижней границе не
    умирает от задержки доставки на сервер (лаг печати ≤3 дн)."""
    near = _c(spec9={**CARD["spec9"], "resolve_by": "2026-11-24"},       # 22 дня от охоты 02.11
              kill_conditions=[{"what": "x", "check_by": "2026-11-10"}])
    late = datetime.date(2026, 11, 5)                                     # сервер на 3 дня позже
    assert not G.check(near, today=late)["pass"]                          # от «сегодня» — 19 дн
    ok, fails, _ = HS.server_check(near, idea_file="idea_2026-11-02_MP.push.txt", today=late,
                                   prices_fn=_prices(last_date="2026-11-04"))
    assert ok, fails


def test_push_card_mismatch_sign_by_header_and_numbers():
    """Перепроверка №2: знак — по ЗАГОЛОВКУ (подстрока «рост» в «роста»/«просто» больше не
    спасает перевёрнутый заголовок); цель, стоп и дата сверки карточки — в тексте."""
    body = ("\n\nЧто делать. Цель $51.44, стоп $35.61, сверка 15 декабря. Без роста просто ждать.\n"
            "Это исследовательский инструмент, не инвестиционная рекомендация.")
    good = "Идея дня — 2 ноября. MP Materials (MP) — добытчик: ставлю на рост." + body
    flipped = "Идея дня — 2 ноября. MP Materials (MP) — добытчик: ставлю на снижение." + body
    silent = "Идея дня — 2 ноября. MP Materials (MP) — добытчик редкоземов." + body
    assert HS.push_card_mismatch(good, CARD) is None
    assert "заголовок" in HS.push_card_mismatch(flipped, CARD)
    assert "однозначно" in HS.push_card_mismatch(silent, CARD)
    assert "цель" in HS.push_card_mismatch(good.replace("51.44", "55.00"), CARD)
    assert "стоп" in HS.push_card_mismatch(good.replace("35.61", "33.00"), CARD)
    assert "даты сверки" in HS.push_card_mismatch(good.replace("15 декабря", "20 декабря"), CARD)
    down = {**CARD, "direction": "вниз"}
    assert HS.push_card_mismatch(flipped, down) is None and HS.push_card_mismatch(good, down)


def test_update_cannot_change_sealed_bet(tmp_path):
    pred = tmp_path / "p.jsonl"
    rec, _ = HS.seal_card(CARD, idea_file=NAME, predictions_path=pred, sealable_fn=lambda a: True)
    assert rec["якорь_цена"] == 39.57 and rec["цель"] == 51.441 and rec["стоп"] == 35.613
    assert HS.update_bet_mismatch(CARD, NAME, predictions_path=pred) is None
    moved = _c(spec9={**CARD["spec9"], "threshold": 47.0})
    assert "порог" in HS.update_bet_mismatch(moved, NAME, predictions_path=pred)
    later = _c(spec9={**CARD["spec9"], "resolve_by": "2026-12-30"})
    assert "дату сверки" in HS.update_bet_mismatch(later, NAME, predictions_path=pred)
    assert HS.update_bet_mismatch(moved, "idea_2026-01-01_ZZ.push.txt", predictions_path=pred) is None


def test_method_score_shows_noise_base(tmp_path):
    pred, outc = tmp_path / "p.jsonl", tmp_path / "o.jsonl"
    card = _c(spec9={**CARD["spec9"], "base_probability": 0.2})
    rec, _ = HS.seal_card(card, idea_file=NAME, predictions_path=pred, sealable_fn=lambda a: True)
    assert rec["вероятность_база"] == 0.2
    outc.write_text(json.dumps({"hash": rec["hash"], "outcome": 0}) + "\n", encoding="utf-8")
    sc = HS.method_score(predictions_path=pred, outcomes_path=outc)
    line = HS.score_line(sc)
    assert "сбылось 0 из 1" in line and "около 0.2" in line and "доверять рано" in line
