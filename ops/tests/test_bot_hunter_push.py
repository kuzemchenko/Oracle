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

GOOD = ("Наблюдение с триггером — проверочная идея.\n\n"
        "Что это значит для тебя. Ничего делать не нужно.\n\n"
        "Что делать. Ждать. Решение за тобой.\n"
        "Это исследовательский инструмент, не инвестиционная рекомендация.")

MD = ("# Исследование\n\nИсточники: https://www.reuters.com/a (2026-10-30), "
      "https://finance.yahoo.com/b (2026-10-31) — каждый несущий факт с датой.\n" * 3)

ODDS = GOOD.replace("Ждать.", "Ждать. Выплата примерно четыре к одному, "
                               "а шанс срыва — около трети.")


def _pres_on():
    return {"hunter_push": True}


def _setup(tmp_path, monkeypatch, files, no_md=()):
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    monkeypatch.setattr(BOT.R, "load_presentation", _pres_on)
    d = tmp_path / "reports" / "ideas"
    d.mkdir(parents=True)
    for name, text in files:
        (d / name).write_text(text, encoding="utf-8")
        if name not in no_md:                        # П8: исследование с источниками рядом
            (d / name.replace(".push.txt", ".md")).write_text(MD, encoding="utf-8")
    b = _bot()
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
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
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-05_F.push.txt", "Жаргон: каскад")])
    b._tick_hunter_ideas()
    assert not b.tg.sent and notes
    f = tmp_path / "reports" / "ideas" / "idea_2026-11-05_F.push.txt"
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
    b, notes = _setup(tmp_path, monkeypatch, [
        ("idea_2026-11-01_A.push.txt", GOOD),
        ("idea_2026-11-02_BAD.push.txt", "Жаргон: каскад"),
        ("idea_2026-11-03_B.push.txt", GOOD.replace("проверочная", "вторая")),
    ])
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
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-04_X.push.txt", GOOD)],
                      no_md=("idea_2026-11-04_X.push.txt",))
    b._tick_hunter_ideas()
    assert not b.tg.sent and notes and "источник" in notes[0]
    b._tick_hunter_ideas()
    assert len(notes) == 1                       # повтор без .md — заметка одна
    (tmp_path / "reports" / "ideas" / "idea_2026-11-04_X.md").write_text(MD, encoding="utf-8")
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
    spam = GOOD.replace("Ждать.", "Ждать. «1 2 3 4 5 6 7 8 9 10 11 12 13 14»." )
    assert any("число-спам" in v for v in PL.lint_hunter_idea(spam))


def test_battle_mp_file_passes_hunter_lint():
    """Боевой файл идеи MP обязан проходить кодовый гейт канала (рамка, без вероятностных
    слов, правила «Идеи дня») — иначе мост молчит и жалоба владельца 02.11 вернётся."""
    p = ROOT / "reports" / "ideas" / "idea_2026-11-01_MP.push.txt"
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
    assert "Прогноз запечатан в журнал: MP закроется выше 45" in text
    assert "Счёт метода охотника" in text
    assert text.rstrip().endswith("не инвестиционная рекомендация.")   # рамка — последней
    datas = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
    assert f"v:q:h_{day}_MP" in datas
    lid = b.state["last_idea_day"]
    assert lid["lead"] == "MP.US" and lid["source"] == "hunter"
    rows = [_json.loads(x) for x in pred.read_text("utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "hunter_idea"


def test_hunter_changed_after_delivery_goes_once_as_update(tmp_path, monkeypatch):
    b, _notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-01_MP.push.txt", GOOD)])
    b.state["hunter_pushed"] = ["idea_2026-11-01_MP.push.txt"]          # легаси: доставлена старая
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and b.tg.sent[0][1].startswith("Обновлённая версия записки")
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1                                          # обновление — один раз


def test_empty_md_is_not_a_source(tmp_path, monkeypatch):
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-06_E.push.txt", GOOD)],
                      no_md=("idea_2026-11-06_E.push.txt",))
    (tmp_path / "reports" / "ideas" / "idea_2026-11-06_E.md").write_text("", encoding="utf-8")
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
    (tmp_path / "reports" / "ideas" / f"idea_{day}_killed.shadow.json").write_text(
        _json.dumps([CARD], ensure_ascii=False), encoding="utf-8")
    b._tick_hunter_followups()
    rows = [_json.loads(x) for x in pred.read_text("utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "hunter_killed" and not b.tg.sent
    b._tick_hunter_followups()
    assert len(pred.read_text("utf-8").splitlines()) == 1          # тень печатается один раз
    rec, _ = HS.seal_card(CARD, idea_file=f"idea_{day}_MP.push.txt", predictions_path=pred,
                          sealable_fn=lambda a: True)
    outc.write_text(_json.dumps({"hash": rec["hash"], "kind": "hunter_idea", "asset": "MP.US",
                                 "direction": "above", "threshold": 45.0, "outcome": 1,
                                 "observed_value": 47.2, "resolve_by": "2026-12-15"}) + "\n",
                    encoding="utf-8")
    b._tick_hunter_followups()
    b._tick_hunter_followups()
    msgs = [t for _c, t, _k in b.tg.sent]
    assert len(msgs) == 1 and "MP закроется выше 45" in msgs[0] and "сбылось" in msgs[0]


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
