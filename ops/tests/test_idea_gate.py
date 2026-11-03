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
         "позже дня охоты"),
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
    assert "цел" in HS.push_card_mismatch(good.replace("51.44", "55.00"), CARD)
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



def test_money_paragraphs_written_by_code_from_prices():
    """Перепроверка №4: абзацы «Почему это ещё не в цене» и «Сколько можно заработать» ПИШЕТ КОД по
    ценам карточки (после reprice — серверным): неверные числа модели заменяются, а не разбираются
    хрупким парсером (прецедент первым, тире диапазона, перевёрнутый знак — больше не брак и не
    лазейка). Дата сверки — с границей числа («5 ноября» ≠ «25 ноября»)."""
    card = _c(entry_limit=40.30, target_price=47.5, stop_price=35.5, trigger_close=48.89,
              trigger_what="объявления продления перемирия", trigger_date="2026-09-23",
              move_since_trigger_pct=-19.06, upside_pct=20.04, downside_pct=10.29,
              precedent={**CARD["precedent"], "amplitude": "+8–31%"},
              spec9={**CARD["spec9"], "threshold": 47.5, "resolve_by": "2026-11-25"})
    txt = ("Идея с триггером — 2 ноября. MP Materials (MP) — добытчик: ставлю на рост.\n\n"
           "Почему это ещё не в цене. В похожем случае ход был 31.5%, а бумага подорожала на 5%.\n\n"
           "Похожий случай. «8–31% за две сессии».\n\n"
           "Сколько можно заработать и чем рискую. Потенциал из прецедента +31%, риск −2%.\n\n"
           "Кто продаёт и почему он ошибается. Принявшие слова за документ.\n\n"
           "Что делать. Вход не дороже $40.30. Цель $47.50, стоп $35.50, крайний выход 25 ноября.\n\n"
           "Это исследовательский инструмент, не инвестиционная рекомендация.")
    assert HS.push_card_mismatch(txt, card) is None             # черновые числа — не повод для брака
    out = HS.apply_rendered(txt, HS.render_numbers(card))
    assert "подешевела на 19% по ценам закрытия" in out and "объявления продления перемирия 23 сентября" in out
    assert "+$90 (+18%)" in out and "−$60 (−12%)" in out and "в полтора раза" in out
    assert "подорожала на 5%" not in out and "+31%, риск −2%" not in out
    assert "рост, как в похожем случае, ещё не начинался" in out           # популярный язык (03.11)
    # абзацев нет — вставляются перед «Кто продаёт»
    bare = txt.replace("Почему это ещё не в цене. В похожем случае ход был 31.5%, а бумага подорожала на 5%.\n\n", "")
    out2 = HS.apply_rendered(bare, HS.render_numbers(card))
    assert out2.index("Почему это ещё не в цене") < out2.index("Кто продаёт")
    assert "даты сверки" in HS.push_card_mismatch(txt.replace("25 ноября", "5 ноября"), card)


def test_preview_cli_matches_bot(tmp_path):
    """Облако гоняет тот же предпросмотр, что применит бот: сверка + подстановка + линт итога."""
    stem = ROOT / "reports" / "ideas" / "idea_2026-11-02_MP"
    res = HS.preview(stem)
    assert res["problems"] == [], res
    assert "подешевела на 19% по ценам закрытия" in res["text"]


def test_future_hunt_date_and_future_facts_rejected():
    ok, fails, _ = HS.server_check(CARD, idea_file="idea_2026-11-09_MP.push.txt", today=TODAY,
                                   prices_fn=_prices())
    assert not ok and "позже сегодняшней" in fails[0]
    rec, why = HS.seal_card(CARD, idea_file="idea_2099-01-01_MP.push.txt",
                            predictions_path=pathlib.Path("/nonexistent/p.jsonl"),
                            sealable_fn=lambda a: True, today=TODAY)
    assert rec is None and "позже сегодняшней" in why
    # факт датирован через 2 дня после охоты 02.11 — брак даже на сервере 05.11
    late = _c(facts=CARD["facts"] + [{"text": "x", "url": "https://a.org/b", "date": "2026-11-04"}])
    ok2, f2, _ = HS.server_check(late, idea_file="idea_2026-11-02_MP.push.txt",
                                 today=datetime.date(2026, 11, 5), prices_fn=_prices(last_date="2026-11-04"))
    assert not ok2 and any("позже дня охоты" in f for f in f2)
    # +1 день допустим (охота перешла через полночь UTC)
    ok3, f3, _ = HS.server_check(_c(facts=CARD["facts"] + [{"text": "x", "url": "https://a.org/b",
                                                            "date": "2026-11-03"}]),
                                 idea_file="idea_2026-11-02_MP.push.txt",
                                 today=datetime.date(2026, 11, 5), prices_fn=_prices(last_date="2026-11-04"))
    assert ok3, f3


def test_score_line_same_population_and_significance(tmp_path):
    small = {"идей": 12, "исходов": 12, "попаданий": 9, "с_базой": 12, "база_ожидание": 3.0,
             "попаданий_с_базой": 9, "p_шум": 0.0004}
    assert "случайно" not in HS.score_line(small)                 # §10: вывод о значимости — с N≥30
    sc = {"идей": 40, "исходов": 40, "попаданий": 20, "с_базой": 40, "база_ожидание": 10.0,
          "попаданий_с_базой": 20, "p_шум": 0.0004}
    assert "это чаще, чем бывает случайно" in HS.score_line(sc)
    sc2 = dict(sc, попаданий=12, попаданий_с_базой=12, p_шум=0.35)
    assert "пока не отличить от случайности" in HS.score_line(sc2)
    sc3 = dict(sc, исходов=12, с_базой=8, попаданий_с_базой=2, база_ожидание=2.5, p_шум=0.7)
    assert "из 8 с посчитанной базой сбылось 2" in HS.score_line(sc3)


def test_render_words_only_and_honest_update_states():
    """Перепроверка №5: асимметрия — только словами (стабильный счёт чисел для линта); новая идея
    «в цене»/за стопом — проблема (не уходит); исправление — честное «план не действует»."""
    assert HS._ratio_words(1.5) == "в полтора раза" and HS._ratio_words(1.7) == "больше чем в полтора раза"
    assert HS._ratio_words(3.02) == "втрое" and HS._ratio_words(1.2) is None
    import re as _re
    for r in (1.5, 1.73, 2.4, 6.0):
        assert not _re.search(r"\d", HS._ratio_words(r))
    # цена ушла за стоп
    broken = _c(spec9={**CARD["spec9"], "last_close": 35.0})
    rend, probs = HS.render_with_problems(broken)
    assert probs and "money" not in rend
    rend_u, probs_u = HS.render_with_problems(broken, update=True)
    assert not probs_u and "план уже не действует: цена уже за стопом" in rend_u["money"]
    assert "+$" not in rend_u["money"]
    # «в цене» — новая идея нельзя, исправление пишет честную фазу
    played = _c(trigger_close=round(39.57 / 1.5, 6))           # +50% против +30–60%
    _r, p_new = HS.render_with_problems(played)
    assert any("отыграно" in x for x in p_new)
    r_upd, p_upd = HS.render_with_problems(played, update=True)
    assert "уже пройдена" in r_upd["why"]
    # «почти не сдвинулась» — тоже ровно одно число
    flat = _c(trigger_close=39.6)
    assert "меньше 1%" in HS.render_numbers(flat)["why"]
    # черновой абзац без кодового — удаляется, числа модели не уходят
    txt = "Шапка: ставлю на рост MP.\n\nСколько можно заработать и чем рискую. +80% (+$400).\n\nЧто делать."
    assert "+80%" not in HS.apply_rendered(txt, {})


def test_regate6_bet_side_entry_and_plan_invalid(tmp_path):
    """Перепроверка №6: исправление не переворачивает сторону; граница входа из карточки обязана быть в
    тексте (точность — до цента); рынок за границей входа — для новой идеи повтор, для исправления —
    «план не действует» и свой «Что делать» (черновой вход не остаётся)."""
    pred = tmp_path / "p.jsonl"
    rec, _ = HS.seal_card(CARD, idea_file=NAME, predictions_path=pred, sealable_fn=lambda a: True)
    flipped = _c(direction="вниз")
    assert "сторону" in HS.update_bet_mismatch(flipped, NAME, predictions_path=pred)
    card = _c(entry_limit=40.30)
    body = ("Идея дня — 2 ноября. MP Materials (MP) — добытчик: ставлю на рост.\n\n"
            "Что это значит для тебя. Сегодня ничего.\n\n"
            "Что делать. Вход не дороже $40.30. Цель $51.44, стоп $35.61, сверка 15 декабря.\n\n"
            "Это исследовательский инструмент, не инвестиционная рекомендация.")
    assert HS.push_card_mismatch(body, card) is None
    assert "границ" in HS.push_card_mismatch(body.replace("$40.30", "$43.90"), card)
    assert "цел" in HS.push_card_mismatch(body.replace("51.44", "51.30"), card)        # 0.3% — уже не цель
    # прицельная проверка 03.11: граница входа — лимит; рынок далеко (>5%) за ней — новая идея ждёт
    ok, fails, _ = HS.server_check(CARD | {"entry_limit": 40.30}, idea_file=f"idea_{TODAY}_MP.push.txt",
                                   today=TODAY, prices_fn=_prices(last=43.5))
    assert not ok and HS.is_transient(fails) and "границей входа" in fails[0]
    # рынок чуть выше границы (лимит ещё может исполниться) — план в силе, исправление не «отменяет» его
    near = _c(entry_limit=40.30, spec9={**CARD["spec9"], "last_close": 41.0})
    final0, probs0 = HS.prepare_push(body, near, update=True)
    assert not probs0 and "план уже не действует" not in final0 and "не дороже $40.30" in final0
    # цена за стопом — план не действует, черновой вход не остаётся
    stopped = _c(entry_limit=40.30, spec9={**CARD["spec9"], "last_close": 35.0})
    final, probs = HS.prepare_push(body, stopped, update=True)
    assert not probs, probs
    assert "прежний план уже не действует: цена уже за стопом" in final and "не дороже $40.30" not in final
    assert "По этому плану сейчас не входить" in final


def test_regate6_words_and_numbers_of_code_paragraphs():
    assert HS._ratio_words(1.46) is None and HS._ratio_words(1.5) == "в полтора раза"
    assert HS._ratio_sentence(2.3) == "Выигрыш превышает возможную потерю больше чем вдвое."
    assert HS._ratio_sentence(1.5) == "Выигрыш в полтора раза больше возможной потери."
    half = _c(trigger_close=round(39.57 / 1.005, 6))                      # ход ровно около 0.5%
    assert "на 0%" not in HS.render_numbers(half)["why"]


def test_regate6_fact_date_kind_and_promise_lint():
    import presentation_lint as PL
    checked = [{**f, "date_kind": "проверка"} for f in CARD["facts"]]
    r = G.check(_c(facts=checked), today=TODAY)
    assert any("фактов с URL и датой 0" in f for f in r["fails"])        # даты проверки — не опора
    t = ("Идея дня. MP (MP) — x: ставлю на рост. Сегодня ничего; напишу 9 ноября. Проверю к 9 ноября.\n"
         "Это исследовательский инструмент, не инвестиционная рекомендация.")
    assert any("обещание сообщения" in v for v in PL.lint_hunter_idea(t))
    assert not any("обещание" in v for v in PL.lint_hunter_idea(t.replace("напишу 9 ноября", "бот напомнит 10 ноября")
                                                                .replace("Проверю к 9 ноября.", "")))



def test_targeted_side_entry_update_and_promise_rules(tmp_path):
    """Прицельная проверка 03.11: «не ставлю на рост, ставлю на снижение» — неоднозначно; граница входа в
    «Что делать» без entry_limit — брак; исправление не меняет цель/стоп/вход записи; разрешены обещания
    кода (напоминание/итог) и «проверю к» в «Что меня опровергнет»."""
    import presentation_lint as PL
    body = ("Идея дня — 2 ноября. MP Materials (MP) — добытчик: больше не ставлю на рост, ставлю на снижение.\n\n"
            "Что делать. Цель $51.44, стоп $35.61, сверка 15 декабря.\n\n"
            "Это исследовательский инструмент, не инвестиционная рекомендация.")
    assert "однозначно" in HS.push_card_mismatch(body, CARD)
    ok_body = body.replace("больше не ставлю на рост, ставлю на снижение", "ставлю на рост")
    assert HS.push_card_mismatch(ok_body, CARD) is None
    assert "entry_limit" in HS.push_card_mismatch(ok_body.replace("Что делать.", "Что делать. Вход не дороже $43.90."), CARD)
    pred = tmp_path / "p.jsonl"
    HS.seal_card(CARD, idea_file=NAME, predictions_path=pred, sealable_fn=lambda a: True)
    assert "стоп" in HS.update_bet_mismatch(_c(stop_price=33.0), NAME, predictions_path=pred)
    assert HS.update_bet_mismatch(CARD, NAME, predictions_path=pred) is None
    t = ("Идея дня. MP (MP) — x: ставлю на рост.\n\nЧто это значит для тебя. 10 ноября пришлю напоминание, "
         "после 25 ноября сообщу итог.\n\nЧто меня опровергнет. Отчёт 5 ноября — проверю к 6 ноября.\n\n"
         "Главный риск — и мой ответ. Сверю 9 ноября.\n"
         "Это исследовательский инструмент, не инвестиционная рекомендация.")
    v = [x for x in PL.lint_hunter_idea(t) if "обещание" in x]
    assert v and "сверю" in v[0] and "пришлю" not in v[0] and "проверю" not in v[0]


def test_chance_line_from_code_and_lint_exempt():
    """Решение владельца 03.11 «да, показывай шанс»: строка шанса — из кода (база шума при reprice),
    в линт не идёт; без серверной базы строки нет."""
    card = _c(spec9={**CARD["spec9"], "base_probability": 0.0682})
    cs = HS.chance_sentence(card)
    assert cs and "примерно в 7 случаях из 100" in cs and "15 декабря" in cs
    assert HS.chance_sentence(CARD) is None
    body = ("Идея дня — 2 ноября. MP Materials (MP) — добытчик: ставлю на рост.\n\n"
            "Что это значит для тебя. Сегодня ничего.\n\n"
            "Что делать. Цель $51.44, стоп $35.61, сверка 15 декабря.\n\n"
            "Это исследовательский инструмент, не инвестиционная рекомендация.")
    final, probs = HS.prepare_push(body, card)
    assert not probs, probs
    assert "случаях из 100" in final and "если войти по последней цене закрытия" in final
