# -*- coding: utf-8 -*-
"""Тик доставки идей охотника (вопрос владельца 02.11: «MP так и не попала в Telegram»).

Закрепляет (ре-гейт №2 02.11): канал за флагом hunter_push; гейт = lint_hunter_idea
(правила «Идеи дня» + рамка «не рекомендация» КОДОМ + запрет вероятностных слов даже
прописью — Инв#6/П16); новый файл уходит один раз (дедуп по имени); битый текст — не
уходит, заметка в дозор; сбой сети в середине цикла НЕ теряет отметки уже доставленных
(state пишется по-событийно — иначе дубль-пуши владельцу); боевой файл MP чист."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import bot as BOT                    # noqa: E402
import presentation_lint as PL       # noqa: E402
from test_bot_v2_loop import _bot    # noqa: E402

GOOD = ("Идея дня — проверочная идея. MP Materials (MP) — добытчик редкоземов: ставлю на рост.\n\n"
        "Что это значит для тебя. Ничего делать не нужно.\n\n"
        "Что делать. Ждать. Цель $51.44, стоп $35.61, сверка 15 декабря. Решение за тобой.\n"
        "Это исследовательский инструмент, не инвестиционная рекомендация.")

MD = ("# Исследование\n\nИсточники: https://www.reuters.com/a (2026-10-30), "
      "https://finance.yahoo.com/b (2026-10-31) — каждый несущий факт с датой.\n" * 3)

ODDS = GOOD.replace("Ждать.", "Ждать. Выплата примерно четыре к одному, "
                               "а шанс срыва — около трети.")


def _pres_on():
    return {"hunter_push": True}


def _setup(tmp_path, monkeypatch, files, no_md=(), no_card=(), verdict="PASS"):
    """Ревью 02.11: новая идея обязана иметь §9-карточку и пройти серверный шлагбаум и суд
    разных семейств — заготовка кладёт карточку к каждому файлу и ставит заглушку суда."""
    import json as _json
    import datetime as _dt
    from ops import hunter_seal as HS, hunter_court as HC
    import orchestrator.universe_resolver as U
    from test_idea_gate import CARD
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    monkeypatch.setattr(BOT.R, "load_presentation", _pres_on)
    monkeypatch.setattr(HS, "MAX_SEAL_LAG_DAYS", 10 ** 6)        # имена тестов датированы жёстко
    # серверные котировки: свежее закрытие «вчера» (возраст цены сервер меряет по своему дню),
    # закрытие в день повода — как в карточке, ряд для σ базы шума
    _last = (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)).isoformat()
    _closes = [39.57 * (1.03 if i % 2 else 0.97) for i in range(60)] + [39.57]
    prices = {"last_close": 39.57, "last_date": _last, "trigger_close": CARD["trigger_close"],
              "closes": _closes}
    monkeypatch.setattr(HS, "_server_prices",
                        lambda a, td=None, con=None: (dict(prices, trigger_date=td)
                                                      if prices.get("last_close") else None))
    monkeypatch.setattr(U, "is_sealable", lambda a, **k: True)
    import idea_gate as G
    from ops import idea_gate as G2
    _today = _dt.date(2026, 11, 2)
    for mod in (G, G2):                                        # шлагбаум считает от даты карточки
        _orig = mod.check
        monkeypatch.setattr(mod, "check", lambda c, today=None, horizon_from=None, _o=_orig:
                            _o(c, today=_today, horizon_from=None))
    calls = []
    monkeypatch.setattr(HC, "judge_card", lambda card, **k: (calls.append(card) or (
        {"verdict": verdict, "objection": "проверить спрос", "evidence": "«x»"} if verdict else None),
        None if verdict else "нет ключа"))
    d = tmp_path / "reports" / "ideas"
    d.mkdir(parents=True)
    for name, text in files:
        (d / name).write_text(text, encoding="utf-8")
        if name not in no_md:                        # П8: исследование с источниками рядом
            (d / name.replace(".push.txt", ".md")).write_text(MD, encoding="utf-8")
        if name not in no_card:
            (d / name.replace(".push.txt", ".card.json")).write_text(
                _json.dumps(CARD, ensure_ascii=False), encoding="utf-8")
    b = _bot()
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
    b._test_prices = prices                                     # тест может «уронить» котировки
    return b, notes


def test_hunter_push_delivers_once_and_lints(tmp_path, monkeypatch):
    b, notes = _setup(tmp_path, monkeypatch, [
        ("idea_2026-11-01_MP.push.txt", GOOD),
        ("idea_2026-11-02_BAD.push.txt", "Жаргон: каскад и edge 🚀"),
        ("idea_2026-11-03_ODDS.push.txt", ODDS),
    ])
    b._tick_hunter_ideas()
    sent = [t for _c, t, _k in b.tg.sent]
    assert any("проверочная идея" in t for t in sent)
    assert not any("каскад" in t for t in sent), "битый текст не должен уходить"
    assert not any("четыре к одному" in t for t in sent), \
        "вероятностные слова без расчёта/печати §9 не должны уходить (Инв#6/П16)"
    assert any("BAD" in n for n in notes) and any("ODDS" in n for n in notes)
    n = len(b.tg.sent)
    b._tick_hunter_ideas()                       # повтор — дедуп (по имени+хешу содержимого)
    assert len(b.tg.sent) == n
    assert sorted(k.split("@")[0] for k in b.state["hunter_pushed"]) == [
        "idea_2026-11-01_MP.push.txt", "idea_2026-11-02_BAD.push.txt",
        "idea_2026-11-03_ODDS.push.txt"]


def test_hunter_fixed_file_redelivered(tmp_path, monkeypatch):
    """Перепроверка №3: брак не хоронит файл навечно — починенная версия (новый хеш)
    доставляется; доставленная версия повторно не уходит."""
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_F.push.txt", "Жаргон: каскад")])
    b._tick_hunter_ideas()
    assert not b.tg.sent and notes
    f = tmp_path / "reports" / "ideas" / "idea_2026-11-02_F.push.txt"
    f.write_text(GOOD, encoding="utf-8")
    b._tick_hunter_ideas()
    assert any("проверочная идея" in t for _c, t, _k in b.tg.sent)
    n = len(b.tg.sent)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == n


def test_hunter_flag_off_sends_nothing(tmp_path, monkeypatch):
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-01_MP.push.txt", GOOD)])
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"hunter_push": False})
    b._tick_hunter_ideas()
    assert not b.tg.sent and not b.state.get("hunter_pushed")


def test_hunter_network_failure_keeps_delivered_marks(tmp_path, monkeypatch):
    """Сбой сети на ВТОРОМ файле: первый уже доставлен и ОТМЕЧЕН (нет дубля на ретрае),
    второй не отмечен (ретрай следующим тиком); заметка о lint-браке не дублируется."""
    import json as _json
    from test_idea_gate import CARD
    b, notes = _setup(tmp_path, monkeypatch, [
        ("idea_2026-11-01_A.push.txt", GOOD),
        ("idea_2026-11-02_BAD.push.txt", "Жаргон: каскад"),
        ("idea_2026-11-03_B.push.txt", GOOD.replace("проверочная", "вторая")
         .replace("MP Materials (MP) — добытчик редкоземов", "CF Industries (CF) — производитель удобрений")),
    ])
    cf = {**CARD, "ticker": "CF.US", "name": "CF Industries", "spec9": {**CARD["spec9"], "asset": "CF.US"}}
    (tmp_path / "reports" / "ideas" / "idea_2026-11-03_B.card.json").write_text(
        _json.dumps(cf, ensure_ascii=False), encoding="utf-8")
    calls = {"n": 0}

    def flaky(text, kb):
        calls["n"] += 1
        return None if "вторая" in text else 1   # сеть падает только на файле B

    monkeypatch.setattr(b, "_send_card", flaky)
    b._tick_hunter_ideas()
    assert sorted(k.split("@")[0] for k in b.state["hunter_pushed"]) == [
        "idea_2026-11-01_A.push.txt", "idea_2026-11-02_BAD.push.txt"]
    assert len(notes) == 1
    b._tick_hunter_ideas()                       # «сеть починилась» — но наш стаб всё ещё падает
    assert calls["n"] == 3, "ретрай только недоставленного файла B (A не дублируется)"
    assert len(notes) == 1, "заметка о том же битом файле не спамится в дозор"


def test_hunter_requires_research_md(tmp_path, monkeypatch):
    """Перепроверка №2 02.11 (П8): пуш без файла исследования с источниками рядом — брак.
    Перепроверка №4: брак не вечный — появился .md → идея доставляется; дозор не спамится."""
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_X.push.txt", GOOD)],
                      no_md=("idea_2026-11-02_X.push.txt",))
    b._tick_hunter_ideas()
    assert not b.tg.sent and notes and "источник" in notes[0]
    b._tick_hunter_ideas()
    assert len(notes) == 1                       # повтор без .md — заметка одна
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_X.md").write_text(MD, encoding="utf-8")
    b._tick_hunter_ideas()
    assert any("проверочная идея" in t for _c, t, _k in b.tg.sent)


def test_odds_wordforms_blocked():
    """Перепроверка №2 02.11: обходы запрета прописью закрыты; величины хода — живут."""
    for bad in ("Выплата один к четырём.", "Сбывается в девяти случаях из десяти.",
                "Вероятно, продлят паузу.", "Срыв в трети случаев.", "Шансы невелики.",
                "Сбывается в 65% случаев.", "Выплата 4 к 1.", "В девяти случаях из 10."):
        assert PL.odds_markers(bad), bad
    assert not PL.odds_markers("Бумага прибавила половину и стоит вдвое ниже пика.")


def test_odds_inside_quotes_blocked_for_hunter():
    """Перепроверка №3: кавычки охотника — его же текст, «Суд сказал: «шанс 65%»» не лазейка."""
    bad = GOOD.replace("Ждать.", "Ждать. Суд сказал: «шанс 65%, четыре к одному».")
    assert any("вероятностные" in v for v in PL.lint_hunter_idea(bad))
    spam = GOOD.replace("Ждать.", "Ждать. «" + " ".join(str(i) for i in range(1, 21)) + "».")
    assert any("число-спам" in v for v in PL.lint_hunter_idea(spam))


def test_battle_mp_file_passes_hunter_lint():
    """Боевой файл идеи дня (MP, охота v3 02.11) обязан проходить кодовый гейт канала и быть
    согласован со своей §9-карточкой — иначе мост молчит и жалоба владельца вернётся."""
    import json as _json
    from ops import hunter_seal as HS
    card = _json.loads((ROOT / "reports" / "ideas" / "idea_2026-11-02_MP.card.json").read_text("utf-8"))
    assert HS.push_card_mismatch((ROOT / "reports" / "ideas" / "idea_2026-11-02_MP.push.txt")
                                 .read_text("utf-8"), card) is None
    p = ROOT / "reports" / "ideas" / "idea_2026-11-02_MP.push.txt"
    viol = PL.lint_hunter_idea(p.read_text(encoding="utf-8"))
    assert viol == [], viol


def test_hunter_push_carries_buttons_footer_and_seals(tmp_path, monkeypatch):
    """Архитектура 02.11: идея охотника — главная идея дня. Перед доставкой карточка §9
    печатается (трек hunter_idea), в пуш КОДОМ дописывается строка о печати и счёт метода,
    под пушем — кнопки V2; «Качаем» работает по этой идее."""
    import json as _json
    from ops import hunter_seal as HS                 # ТО ЖЕ имя модуля, что импортирует бот
    sys.path.insert(0, str(ROOT / "ops" / "tests"))
    from test_idea_gate import CARD
    pred = tmp_path / "pred.jsonl"
    monkeypatch.setattr(HS, "PRED", pred)
    monkeypatch.setattr(HS, "OUTC", tmp_path / "out.jsonl")
    import orchestrator.universe_resolver as U
    monkeypatch.setattr(U, "is_sealable", lambda a, **k: True)
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).date().isoformat()   # печать задним числом запрещена
    b, notes = _setup(tmp_path, monkeypatch, [(f"idea_{day}_MP.push.txt", GOOD)])
    (tmp_path / "reports" / "ideas" / f"idea_{day}_MP.card.json").write_text(
        _json.dumps(CARD, ensure_ascii=False), encoding="utf-8")
    b._tick_hunter_ideas()
    assert not notes, notes
    _c, text, kb = b.tg.sent[-1]
    assert "Записал в журнал проверку: закроется ли MP выше 45" in text
    assert "Счёт метода охотника" in text
    assert text.rstrip().endswith("не инвестиционная рекомендация.")   # рамка — последней
    datas = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
    assert f"v:q:h_{day}_MP" in datas
    lid = b.state["last_idea_day"]
    assert lid["lead"] == "MP.US" and lid["source"] == "hunter"
    rows = [_json.loads(x) for x in pred.read_text("utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "hunter_idea"


def test_hunter_changed_after_delivery_goes_once_as_update(tmp_path, monkeypatch):
    """Перепроверка 02.11: «обновлённая версия» — только для версии, доставленной НОВЫМ кодом
    (явный список hunter_delivered); без суда и без второй ставки. Легаси-отметка «просто имя»
    (старый код так помечал и доставку, и брак) — файл не пересылается вовсе."""
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-01_MP.push.txt", GOOD)])
    b.state["hunter_delivered"] = ["idea_2026-11-01_MP.push.txt@0000000000"]   # доставлена прежняя версия
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and b.tg.sent[0][1].startswith("Обновлённая версия записки")
    assert "новой ставки не делаю" in b.tg.sent[0][1]
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1                                          # обновление — один раз
    b2, _n2 = _setup(tmp_path / "legacy", monkeypatch, [("idea_2026-11-01_MP.push.txt", GOOD)])
    b2.state["hunter_pushed"] = ["idea_2026-11-01_MP.push.txt"]
    b2._tick_hunter_ideas()
    assert not b2.tg.sent


def test_empty_md_is_not_a_source(tmp_path, monkeypatch):
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_E.push.txt", GOOD)],
                      no_md=("idea_2026-11-02_E.push.txt",))
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_E.md").write_text("", encoding="utf-8")
    b._tick_hunter_ideas()
    assert not b.tg.sent and notes


def test_server_observation_not_pushed_as_lead_but_idea_is(tmp_path, monkeypatch):
    """Архитектура 02.11: утренняя «Наблюдение дня» (защищать нечего) главным пушем не уходит,
    прогон помечен обработанным; защищаемая «Идея дня» от сервера уходит как раньше."""
    import bot_state as S
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    pres = {"send_mock_to_telegram": False, "idea_day_lead": True, "top3_lead": True,
            "daily_case_lead": True, "server_observation_push": False,
            "mode": "short", "still_unclear": set()}
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: pres)
    monkeypatch.setattr(BOT.R, "money_ideas_from_protocol", lambda p: [])
    monkeypatch.setattr(BOT.W, "ingest_protocol", lambda p, existing_ids=None: [])
    for genre, head, expect_push in (("наблюдение", "Наблюдение дня — текст.", False),
                                     ("идея", "Идея дня — текст.", True)):
        proto = {"run_id": f"ef_{genre}", "ts": "2026-11-03T09:00:00+00:00", "mode": "live"}
        monkeypatch.setattr(BOT.R, "new_runs", lambda pushed, _p=proto: [_p] if _p["run_id"] not in pushed else [])
        monkeypatch.setattr(BOT.R, "idea_of_day_from_protocol",
                            lambda p, _h=head, _g=genre, **kw: (_h, {"idx": {"1": "LNG.US"},
                                                                     "lead": "LNG.US", "genre": _g}, []))
        b = _bot()
        b._tick_reports()
        sent = [t for _c, t, _k in b.tg.sent]
        assert (any(head in t for t in sent)) is expect_push, (genre, sent)
        assert proto["run_id"] in b.state["digest_sent"]
        if not expect_push:
            assert not sent, "наблюдение не должно уходить и фолбэком (Топ-3/Разбор дня)"


def test_buttons_paper_and_skip_reason_write_decisions(tmp_path, monkeypatch):
    """Разбор 02.11 (метрика «отклик»): «Беру на бумагу» → action=paper; «Мимо: не верю» →
    skip + отдельная строка мотива; повторное нажатие — дедуп."""
    import json as _json
    import bot_state as S
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    for code, action in (("p", "paper"), ("m2", "skip")):
        b = _bot()
        b.state["last_idea_day"] = {"run_id": "h_x", "lead": "MP.US", "idx": {},
                                    "issued_at": "2026-11-02T11:00:00+00:00", "source": "hunter"}
        b._on_callback({"id": "c", "data": f"v:{code}:h_x",
                        "message": {"message_id": 5, "chat": {"id": 555}}})
    rows = [_json.loads(x) for x in (tmp_path / "decisions_user.jsonl").read_text("utf-8").splitlines()]
    assert [r.get("action") for r in rows if r["type"] == "decision"] == ["paper", "skip"]
    assert any(r["type"] == "motive" and r["motive"] == "не верю" for r in rows)
    kb = BOT.Bot._idea_kb("h_x")
    labels = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
    assert "Мимо: не понял" in labels and "📝 Беру на бумагу" in labels
    assert all(len(btn["callback_data"].encode()) <= 64 for row in kb["inline_keyboard"] for btn in row)


def test_shadows_sealed_silently_and_outcome_pushed(tmp_path, monkeypatch):
    """Тени отвергнутых печатаются в hunter_killed без пуша; сверенная идея охотника →
    сообщение «сбылось/не сбылось» один раз."""
    import json as _json
    from ops import hunter_seal as HS
    sys.path.insert(0, str(ROOT / "ops" / "tests"))
    from test_idea_gate import CARD
    pred, outc = tmp_path / "pred.jsonl", tmp_path / "out.jsonl"
    monkeypatch.setattr(HS, "PRED", pred)
    monkeypatch.setattr(HS, "OUTC", outc)
    import orchestrator.universe_resolver as U
    monkeypatch.setattr(U, "is_sealable", lambda a, **k: True)
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    b, _notes = _setup(tmp_path, monkeypatch, [])
    other = {**CARD, "ticker": "CF.US", "spec9": {**CARD["spec9"], "asset": "CF.US"}}
    (tmp_path / "reports" / "ideas" / f"idea_{day}_killed.shadow.json").write_text(
        _json.dumps([CARD, other], ensure_ascii=False), encoding="utf-8")
    b._tick_hunter_followups()
    rows = [_json.loads(x) for x in pred.read_text("utf-8").splitlines()]
    assert len(rows) == 2 and {r["asset"] for r in rows} == {"MP.US", "CF.US"}   # ВСЕ тени
    assert all(r["kind"] == "hunter_killed" for r in rows) and not b.tg.sent
    b._tick_hunter_followups()
    assert len(pred.read_text("utf-8").splitlines()) == 2          # тень печатается один раз
    rec, _ = HS.seal_card(CARD, idea_file=f"idea_{day}_MP.push.txt", predictions_path=pred,
                          sealable_fn=lambda a: True)
    outc.write_text(_json.dumps({"hash": rec["hash"], "kind": "hunter_idea", "asset": "MP.US",
                                 "direction": "above", "threshold": 45.0, "outcome": 1,
                                 "observed_value": 47.2, "resolve_by": "2026-12-15"}) + "\n",
                    encoding="utf-8")
    b._tick_hunter_followups()
    b._tick_hunter_followups()
    msgs = [t for _c, t, _k in b.tg.sent]
    assert len(msgs) == 1 and "закроется ли MP выше 45" in msgs[0] and "сбылось" in msgs[0]


def test_stale_card_not_sealed_retroactively(tmp_path):
    """П16: карточка охоты старше 3 дней не печатается — это было бы задним числом."""
    import datetime as _dt
    from ops import hunter_seal as HS
    sys.path.insert(0, str(ROOT / "ops" / "tests"))
    from test_idea_gate import CARD
    rec, why = HS.seal_card(CARD, idea_file="idea_2026-10-20_MP.push.txt",
                            predictions_path=tmp_path / "p.jsonl", sealable_fn=lambda a: True,
                            today=_dt.date(2026, 11, 2))
    assert rec is None and "задним числом" in why


def test_court_fatal_blocks_and_shadows(tmp_path, monkeypatch):
    """П10 (ревью 02.11): суд разных семейств FATAL → идея не уходит, пишется тенью, дозор."""
    import json as _json
    from ops import hunter_seal as HS
    pred = tmp_path / "pred.jsonl"
    monkeypatch.setattr(HS, "PRED", pred)
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)], verdict="FATAL")
    b._tick_hunter_ideas()
    assert not b.tg.sent and any("отверг суд разных семейств" in n for n in notes)
    rows = [_json.loads(x) for x in pred.read_text("utf-8").splitlines()]
    assert rows and rows[0]["kind"] == "hunter_killed"


def test_court_unavailable_fail_closed_retry_hourly(tmp_path, monkeypatch):
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)], verdict=None)
    b._tick_hunter_ideas()
    b._tick_hunter_ideas()
    assert not b.tg.sent
    assert sum("суд разных семейств сейчас недоступен" in n for n in notes) == 1


def test_court_fix_adds_second_check_line(tmp_path, monkeypatch):
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)], verdict="FIX")
    b._tick_hunter_ideas()
    text = b.tg.sent[-1][1]
    assert "Вторая проверка (модели других разработчиков, вслепую)" in text and "проверить спрос" in text
    assert text.rstrip().endswith("не инвестиционная рекомендация.")


def test_new_idea_without_card_or_failing_server_gate_not_sent(tmp_path, monkeypatch):
    import json as _json
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_A.push.txt", GOOD),
                                              ("idea_2026-11-02_B.push.txt", GOOD.replace("проверочная", "вторая"))],
                      no_card=("idea_2026-11-02_A.push.txt",))
    from test_idea_gate import CARD
    bad = {**CARD, "catalyst": {"what": "x", "date": "2027-06-01"}}   # катализатор вне окна
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_B.card.json").write_text(
        _json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    b._tick_hunter_ideas()
    assert not b.tg.sent
    assert any("без §9-карточки" in n for n in notes) and any("серверный шлагбаум" in n for n in notes)


def test_more_ideas_under_hunter_uses_server_top3(tmp_path, monkeypatch):
    b, _notes = _setup(tmp_path, monkeypatch, [])
    b.state["last_idea_day"] = {"run_id": "h_2026-11-02_MP", "lead": "MP.US", "source": "hunter"}
    b.state["last_top3"] = {"run_id": "ef_srv", "idx": {}}
    asked = []
    monkeypatch.setattr(BOT.R, "load_protocol", lambda rid: asked.append(rid) or None)
    b._cmd_more_ideas(555)
    assert asked == ["ef_srv"]


def test_watchdog_one_honest_message_when_no_idea_by_13(tmp_path, monkeypatch):
    import datetime as _dt
    b, notes = _setup(tmp_path, monkeypatch, [])
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"server_observation_push": False})
    early = _dt.datetime(2026, 11, 2, 13, 5, tzinfo=_dt.timezone.utc)
    b._tick_idea_day_watchdog(early)                 # перепроверка №3: до штатной доставки — молчим
    assert not b.tg.sent
    mon13 = _dt.datetime(2026, 11, 2, BOT.WATCHDOG_HOUR_UTC, 5, tzinfo=_dt.timezone.utc)
    b._tick_idea_day_watchdog(mon13)
    b._tick_idea_day_watchdog(mon13)
    msgs = [t for _c, t, _k in b.tg.sent]
    assert len(msgs) == 1 and "пока не готова" in msgs[0] and notes
    assert "пришлю, как только пройдёт" not in msgs[0]          # без невыполнимых обещаний
    b2, _n2 = _bot(), None
    b2.state["idea_day_delivered"] = "2026-11-02"
    b2._tick_idea_day_watchdog(mon13)
    assert not b2.tg.sent


def test_honest_zero_is_not_an_idea_genre():
    import bot_reports as R
    proto = {"run_id": "ef_z", "ts": "2026-11-03T09:00:00+00:00", "граф_отбор": {"топ_k": []}}
    text, meta, _v = R.idea_of_day_from_protocol(proto)
    if text and "Сегодня стоящего нет" in text:
        assert meta["genre"] == "наблюдение"


def test_quiet_restart_on_new_code_only_if_compiles(tmp_path, monkeypatch):
    """Ревью 02.11: ежечасный pull приносит код — бот тихо перезапускается, но только если
    новый код компилируется (иначе цикл падений); битый код → заметка в дозор."""
    b, notes = _setup(tmp_path, monkeypatch, [])
    monkeypatch.setattr(BOT, "_code_signature", lambda: (2, 2.0))
    monkeypatch.setattr(BOT, "_code_files", lambda: [])
    assert b._maybe_restart_on_new_code((1, 1.0)) is True and b.state["quiet_restart"] is True
    assert b._maybe_restart_on_new_code((2, 2.0)) is False          # подпись та же — работаем
    broken = tmp_path / "broken.py"
    broken.write_text("def x(:\n", encoding="utf-8")
    monkeypatch.setattr(BOT, "_code_files", lambda: [broken])
    b.state.pop("quiet_restart", None)
    assert b._maybe_restart_on_new_code((1, 1.0)) is False and notes and "не компилируется" in notes[-1]


def test_nocard_noted_once_and_unsealable_new_idea_not_sent(tmp_path, monkeypatch):
    """Перепроверка 02.11: «нет карточки» — одна заметка, не спам каждым тиком; новая идея, которую
    не удалось запечатать, не уходит (печать до доставки, П16)."""
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_A.push.txt", GOOD)],
                      no_card=("idea_2026-11-02_A.push.txt",))
    for _ in range(5):
        b._tick_hunter_ideas()
    assert len(notes) == 1 and not b.tg.sent
    import orchestrator.universe_resolver as U
    b2, notes2 = _setup(tmp_path / "u", monkeypatch, [("idea_2026-11-02_B.push.txt", GOOD)])
    monkeypatch.setattr(U, "is_sealable", lambda a, **k: False)
    b2._tick_hunter_ideas()
    assert not b2.tg.sent and any("не запечатан" in n for n in notes2)


def test_second_check_line_is_linted():
    """Сырой текст судьи другого семейства идёт после линта пуша — линтуется сам."""
    from ops import hunter_court as HC
    assert HC.second_check_line({"objection": "шанс провала около 70%, вероятно"}) == HC.GENERIC_LINE
    assert HC.second_check_line({"objection": "см. wf_abcdef12 и 1 2 3 4"}) == HC.GENERIC_LINE
    ok = HC.second_check_line({"objection": "спрос на магниты может не подтвердиться отчётом"})
    assert "спрос на магниты" in ok


def test_update_of_delivered_idea_skips_seal_window_but_not_court(tmp_path, monkeypatch):
    """Исправление доставленной идеи через неделю: без шлагбаума новой ставки (печати нет),
    но через суд разных семейств и сверку с карточкой."""
    from ops import hunter_seal as HS
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)], verdict="PASS")
    b.state["hunter_delivered"] = ["idea_2026-11-02_MP.push.txt@0000000000"]
    monkeypatch.setattr(HS, "server_check", lambda *a, **k: (_ for _ in ()).throw(AssertionError("не нужен")))
    b._tick_hunter_ideas()
    assert b.tg.sent and b.tg.sent[0][1].startswith("Обновлённая версия записки")


def test_transient_price_failure_retries_instead_of_burying(tmp_path, monkeypatch):
    """Перепроверка №2: временный сбой котировок (БД занята, EODHD не ответил) не хоронит идею
    навсегда — повтор через час, одна заметка; котировки вернулись — идея уходит."""
    import datetime as _dt
    import bot_state as S
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])
    b._test_prices["last_close"] = None
    b._tick_hunter_ideas()
    b._tick_hunter_ideas()
    assert not b.tg.sent and len(notes) == 1 and "ждёт серверных котировок" in notes[0]
    assert not any(k.endswith(":bad") for k in b.state.get("hunter_pushed") or [])
    b._test_prices["last_close"] = 39.57
    b._tick_hunter_ideas()
    assert not b.tg.sent                                         # час ещё не прошёл
    later = S.now_utc() + _dt.timedelta(minutes=61)
    monkeypatch.setattr(S, "now_utc", lambda: later)
    b._tick_hunter_ideas()
    assert any("проверочная идея" in t for _c, t, _k in b.tg.sent)


def test_flipped_header_not_sent(tmp_path, monkeypatch):
    flipped = GOOD.replace("ставлю на рост", "ставлю на снижение")
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", flipped)])
    b._tick_hunter_ideas()
    assert not b.tg.sent and any("заголовок" in n for n in notes)


def test_update_changing_sealed_bet_blocked(tmp_path, monkeypatch):
    """Исправление доставленной идеи с другим порогом — не уходит (в журнале другая ставка)."""
    import json as _json
    from ops import hunter_seal as HS
    from test_idea_gate import CARD
    pred = tmp_path / "pred.jsonl"
    monkeypatch.setattr(HS, "PRED", pred)
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])
    HS.seal_card(CARD, idea_file="idea_2026-11-02_MP.push.txt", predictions_path=pred,
                 sealable_fn=lambda a: True, today=__import__("datetime").date(2026, 11, 2))
    b.state["hunter_delivered"] = ["idea_2026-11-02_MP.push.txt@0000000000"]
    moved = {**CARD, "spec9": {**CARD["spec9"], "threshold": 47.0}}
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_MP.card.json").write_text(
        _json.dumps(moved, ensure_ascii=False), encoding="utf-8")
    b._tick_hunter_ideas()
    assert not b.tg.sent and any("меняет порог" in n for n in notes)


def test_buttons_of_each_hunter_idea_live_and_kachaem_shows_objection(tmp_path, monkeypatch):
    """Перепроверка №2: у каждой идеи охотника свои кнопки (вторая идея не убивает кнопки первой);
    «Качаем» показывает сомнение второй проверки, которое обещала строка «вторая проверка»."""
    import bot_state as S
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    from test_idea_gate import CARD
    import json as _json
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-01_MP.push.txt", GOOD),
                                               ("idea_2026-11-02_CF.push.txt", GOOD.replace(
                                                   "MP Materials (MP) — добытчик редкоземов",
                                                   "CF Industries (CF) — производитель удобрений"))],
                       verdict="FIX")
    cf = {**CARD, "ticker": "CF.US", "name": "CF Industries", "spec9": {**CARD["spec9"], "asset": "CF.US"}}
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_CF.card.json").write_text(
        _json.dumps(cf, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(b, "_cmd_check_idea", lambda chat, lead: None)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 2
    assert b.state["last_idea_day"]["run_id"] == "h_2026-11-02_CF"
    n = len(b.tg.sent)
    b._on_callback({"id": "c", "data": "v:q:h_2026-11-01_MP",
                    "message": {"message_id": 5, "chat": {"id": 555}}})
    after = [t for _c, t, _k in b.tg.sent[n:]]
    assert not any("устарела" in t for t in after)
    assert any("Сомнение второй проверки" in t and "проверить спрос" in t for t in after)
    assert b.state["hunter_lids"]["h_2026-11-01_MP"]["action"] == "deep_dive"
    assert not b.state["last_idea_day"].get("action")            # текущая идея дня не тронута


def test_shadow_transient_failure_retried(tmp_path, monkeypatch):
    import json as _json
    import datetime as _dt
    import bot_state as S
    from ops import hunter_seal as HS
    from test_idea_gate import CARD
    pred = tmp_path / "pred.jsonl"
    monkeypatch.setattr(HS, "PRED", pred)
    monkeypatch.setattr(HS, "OUTC", tmp_path / "out.jsonl")
    b, _notes = _setup(tmp_path, monkeypatch, [])
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_killed.shadow.json").write_text(
        _json.dumps([CARD], ensure_ascii=False), encoding="utf-8")
    b._test_prices["last_close"] = None
    b._tick_hunter_followups()
    assert not pred.exists() and "idea_2026-11-02_killed.shadow.json" not in (b.state.get("hunter_shadow") or [])
    b._test_prices["last_close"] = 39.57
    later = S.now_utc() + _dt.timedelta(minutes=61)
    monkeypatch.setattr(S, "now_utc", lambda: later)
    b._tick_hunter_followups()
    rows = [_json.loads(x) for x in pred.read_text("utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "hunter_killed"


def test_git_pull_alert_dedup_only_after_delivery(tmp_path, monkeypatch):
    import json as _json
    b, _notes = _setup(tmp_path, monkeypatch, [])
    j = tmp_path / "journal"
    j.mkdir(exist_ok=True)
    (j / "alerts.jsonl").write_text(_json.dumps({"label": "git_pull", "exit": 3,
                                                 "ts": "2026-11-03T10:05:00Z"}) + "\n", encoding="utf-8")
    real = b.tg.send_message
    monkeypatch.setattr(b.tg, "send_message", lambda *a, **k: None)
    b._tick_alerts()
    assert not b.state.get("git_pull_alert_day")
    monkeypatch.setattr(b.tg, "send_message", real)
    b._tick_alerts()
    assert any("не смог забрать обновления" in t for _c, t, _k in b.tg.sent)
    assert b.state.get("git_pull_alert_day") == "2026-11-03:3"         # дедуп — по дню И коду


def test_selfcheck_imports_hunter_modules(capsys):
    assert BOT.selfcheck() == 0
    assert "модули охоты: OK" in capsys.readouterr().out


def test_raw_court_odds_never_reach_owner(tmp_path, monkeypatch):
    """Перепроверка №3 (блокер): сырое возражение судьи с шансами не уходит владельцу ни строкой
    в пуше, ни по кнопке «Качаем», ни в заметке дозора о FATAL."""
    import bot_state as S
    from ops import hunter_court as HC
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)], verdict="FIX")
    monkeypatch.setattr(HC, "judge_card", lambda card, **k: (
        {"verdict": "FIX", "objection": "шанс провала около 70%, вероятно", "evidence": ""}, None))
    monkeypatch.setattr(b, "_cmd_check_idea", lambda chat, lead: None)
    b._tick_hunter_ideas()
    b._on_callback({"id": "c", "data": "v:q:h_2026-11-02_MP",
                    "message": {"message_id": 5, "chat": {"id": 555}}})
    allt = " ".join(t for _c, t, _k in b.tg.sent)
    assert "70%" not in allt and "шанс провала" not in allt
    assert HC.GENERIC_LINE in allt and "Качаем" not in HC.GENERIC_LINE
    b2, notes2 = _setup(tmp_path / "f", monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])
    monkeypatch.setattr(HC, "judge_card", lambda card, **k: (
        {"verdict": "FATAL", "point": 1, "objection": "шанс 65% что продлят", "evidence": "x"}, None))
    b2._tick_hunter_ideas()
    assert notes2 and not any("65%" in n for n in notes2) and any("пункт рубрики 1" in n for n in notes2)


def test_new_version_after_failed_delivery_must_keep_sealed_bet(tmp_path, monkeypatch):
    """Перепроверка №3: печать прошла, доставка сорвалась, пришла новая версия с ДРУГОЙ ставкой —
    она не уходит с подписью «уже записан» (в журнале другая ставка)."""
    import json as _json
    from ops import hunter_seal as HS
    from test_idea_gate import CARD
    pred = tmp_path / "pred.jsonl"
    monkeypatch.setattr(HS, "PRED", pred)
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])
    monkeypatch.setattr(b, "_send_card", lambda text, kb: None)        # сеть упала после печати
    b._tick_hunter_ideas()
    assert pred.exists() and not b.tg.sent
    moved = {**CARD, "spec9": {**CARD["spec9"], "threshold": 47.0}}
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_MP.card.json").write_text(
        _json.dumps(moved, ensure_ascii=False), encoding="utf-8")
    sent = []
    monkeypatch.setattr(b, "_send_card", lambda text, kb: sent.append(text) or 1)
    b._tick_hunter_ideas()
    assert not sent and any("меняет порог" in n for n in notes)


def test_delivered_text_has_code_money_paragraphs(tmp_path, monkeypatch):
    """Перепроверка №4: абзацы с деньгами в ДОСТАВЛЕННОМ пуше — текст кода по серверным ценам."""
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])
    b._test_prices["last_close"] = 39.57
    b._tick_hunter_ideas()
    text = b.tg.sent[-1][1]
    assert "Сколько можно заработать и чем рискую. На пятьсот долларов" in text
    assert "+$150 (+30%)" in text and "−$50 (−10%)" in text and "втрое" in text
    assert text.index("Сколько можно заработать") < text.index("Что делать")


def test_kachaem_for_hunter_shows_dossier_not_old_funnel(tmp_path, monkeypatch):
    """Перепроверка №4: «Качаем» под идеей охотника не гонит её в старую воронку со старым судом
    («зарезали» прямо под идеей) — показывает досье из карточки; фрагменты с шансами отброшены."""
    import json as _json
    import bot_state as S
    from test_idea_gate import CARD
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])
    card = {**CARD, "facts": CARD["facts"] + [{"text": "шанс продления 70%", "url": "https://x.org/a",
                                                "date": "2026-10-30"}]}
    (tmp_path / "reports" / "ideas" / "idea_2026-11-02_MP.card.json").write_text(
        _json.dumps(card, ensure_ascii=False), encoding="utf-8")
    called = []
    monkeypatch.setattr(b, "_cmd_check_idea", lambda chat, lead: called.append(lead))
    b._tick_hunter_ideas()
    n = len(b.tg.sent)
    b._on_callback({"id": "c", "data": "v:q:h_2026-11-02_MP",
                    "message": {"message_id": 5, "chat": {"id": 555}}})
    after = " ".join(t for _c, t, _k in b.tg.sent[n:])
    assert not called, "старая воронка под идеей охотника не запускается"
    assert "вот на чём стоит идея" in after and "Факты с источниками" in after
    assert "jonesday.com" in after and "70%" not in after
    assert "не инвестиционная рекомендация" in after


def test_broken_card_does_not_block_following_ideas(tmp_path, monkeypatch):
    """Перепроверка №4: карточка со spec9-строкой роняла весь тик — следующие идеи не шли никогда."""
    import json as _json
    from test_idea_gate import CARD
    b, notes = _setup(tmp_path, monkeypatch, [
        ("idea_2026-11-01_ZZ.push.txt", GOOD.replace("MP Materials (MP)", "Zeta (ZZ) и MP")),
        ("idea_2026-11-02_MP.push.txt", GOOD)])
    (tmp_path / "reports" / "ideas" / "idea_2026-11-01_ZZ.card.json").write_text(
        _json.dumps({**CARD, "ticker": "ZZ.US", "spec9": "битая"}, ensure_ascii=False), encoding="utf-8")
    b._tick_hunter_ideas()
    assert any("проверочная идея" in t and "Zeta" not in t for _c, t, _k in b.tg.sent)
    assert any("ZZ" in n for n in notes)


def test_fork_reminder_on_catalyst_day_once(tmp_path, monkeypatch):
    """Перепроверка №4: в день катализатора код сам напоминает условие и план из записки (обещание
    «напишу перед открытием» больше не висит без исполнителя); один раз; «Мимо» — без напоминания."""
    import datetime as _dt
    b, _notes = _setup(tmp_path, monkeypatch, [])
    b.state["hunter_lids"] = {
        "h_2026-11-02_MP": {"run_id": "h_2026-11-02_MP", "lead": "MP.US", "source": "hunter",
                            "sealed": True, "name": "MP Materials", "catalyst_date": "2026-11-10",
                            "header": "Идея с триггером — … ставлю на рост, если Пекин не продлит.",
                            "todo": "Что делать. Вход не дороже $40.30."},
        "h_2026-11-03_X": {"run_id": "h_2026-11-03_X", "lead": "X.US", "source": "hunter",
                           "sealed": True, "catalyst_date": "2026-11-10", "action": "skip"}}
    early = _dt.datetime(2026, 11, 10, 9, 0, tzinfo=_dt.timezone.utc)
    b._tick_hunter_fork_reminders(early)
    assert not b.tg.sent
    noon = _dt.datetime(2026, 11, 10, 12, 30, tzinfo=_dt.timezone.utc)
    b._tick_hunter_fork_reminders(noon)
    b._tick_hunter_fork_reminders(noon)
    msgs = [t for _c, t, _k in b.tg.sent]
    assert len(msgs) == 1 and "день развилки по идее MP Materials" in msgs[0]
    assert "Вход не дороже $40.30" in msgs[0] and "не инвестиционная рекомендация" in msgs[0]


def test_update_repriced_by_server_and_waits_on_missing_prices(tmp_path, monkeypatch):
    """Перепроверка №4: исправление доставленной идеи — цифры по серверным ценам; нет котировок —
    повтор, не брак."""
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])
    b.state["hunter_delivered"] = ["idea_2026-11-02_MP.push.txt@0000000000"]
    b._test_prices["last_close"] = None
    b._tick_hunter_ideas()
    assert not b.tg.sent and any("ждёт серверных котировок" in n for n in notes)
    assert not any(k.endswith(":bad") for k in b.state.get("hunter_pushed") or [])


def test_final_text_checked_before_seal_nothing_sealed_on_reject(tmp_path, monkeypatch):
    """Перепроверка №5 (блокер): итоговый текст (после подстановки кодом) проверяется ДО печати —
    отбракованная идея не попадает в журнал (не портит счёт и не блокирует бумагу)."""
    from ops import hunter_seal as HS
    pred = tmp_path / "pred.jsonl"
    monkeypatch.setattr(HS, "PRED", pred)
    # цель и стоп есть ТОЛЬКО в черновом абзаце о деньгах — после подстановки кода их в тексте нет
    draft = GOOD.replace("Что делать. Ждать. Цель $51.44, стоп $35.61, сверка 15 декабря. Решение за тобой.",
                         "Сколько можно заработать и чем рискую. Цель $51.44, стоп $35.61.\n\n"
                         "Что делать. Ждать до 15 декабря. Решение за тобой.")
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", draft)])
    b._tick_hunter_ideas()
    assert not b.tg.sent and not pred.exists(), "ничего не запечатано"
    assert any("итогового текста" in n and "цель" in n for n in notes)


def test_environment_error_is_retry_not_bad(tmp_path, monkeypatch):
    """Перепроверка №5: сбой окружения (лок журнала, база) — повтор, а не вечный брак."""
    from ops import hunter_seal as HS
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-02_MP.push.txt", GOOD)])

    def boom(*a, **k):
        raise OSError("журнал занят")
    monkeypatch.setattr(HS, "seal_card", boom)
    b._tick_hunter_ideas()
    assert not b.tg.sent
    assert not any(k.endswith(":bad") for k in b.state.get("hunter_pushed") or [])
    assert b.state.get("hunter_retry") and any("сбоя окружения" in n for n in notes)
