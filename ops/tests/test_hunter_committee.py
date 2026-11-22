# -*- coding: utf-8 -*-
"""Q-комитет-в-идее-охотника: короткий блок «Комитет: X из 10 — решение» под идеей охотника в Telegram.

Закрепляет: (1) всё за флагом hunter_committee_push, который читается НАПРЯМУЮ из config/presentation.yaml;
при выключенном флаге сообщение идеи прежнее байт в байт, комитет не считается и ничего не пишет; (2) блок
пишет код уже после проверки слов модели (в линт не попадает) и встаёт перед строкой-рамкой; (3) вердикт
записывается ДО исхода один раз на идею — новая версия записки дубль не пишет; (4) сбой комитета идею не
задерживает; (5) сообщение не вырастает за лимит Telegram: блок сокращается, идея не теряется; (6) балл —
пометка, а не ворота: идея с доводом суда против показывается «для обсуждения» вместе с баллом.

Герметично: суд, котировки, Telegram и состояние бота подставные; запись вердиктов и источники сектора/ставок
уведены во временную папку предохранителем ops/tests/conftest.py."""
import datetime as _dt
import json as _json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import bot as BOT                    # noqa: E402
import bot_chat as CH                # noqa: E402
import presentation_lint as PL       # noqa: E402
from ops import idea_verdict as IV   # noqa: E402
from test_bot_v2_loop import _bot    # noqa: E402
from test_idea_gate import CARD      # noqa: E402

GOOD = ("Идея дня — проверочная идея. MP Materials (MP) — добытчик редкоземов: ставлю на рост.\n\n"
        "Что это значит для тебя. Ничего делать не нужно.\n\n"
        "Что делать. Ждать. Цель $51.44, стоп $35.61, сверка 15 декабря. Решение за тобой.\n"
        "Это исследовательский инструмент, не инвестиционная рекомендация.")
MD = ("# Исследование\n\nИсточники: https://www.reuters.com/a (2026-10-30), "
      "https://finance.yahoo.com/b (2026-10-31) — каждый несущий факт с датой.\n" * 3)
NAME = "idea_2026-11-01_MP.push.txt"
DAY = _dt.date(2026, 11, 2)
OLD_FOOT_KEYS = {"lines", "asset", "sealed", "objection", "text", "name", "catalyst_date", "header", "todo"}


def _iv_mods():
    import importlib
    return [importlib.import_module(n) for n in ("ops.idea_verdict", "idea_verdict")]


def _flag(tmp_path, value=True):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "presentation.yaml").write_text(
        "hunter_push: true\n" + ("" if value is None else f"hunter_committee_push: {str(value).lower()}\n"),
        encoding="utf-8")


def _setup(tmp_path, monkeypatch, files=((NAME, GOOD),), verdict="PASS", flag=True):
    """Как в test_bot_hunter_push: карточка к каждому файлу, серверные котировки, шлагбаум от даты карточки,
    заглушка суда разных семейств, подставной Telegram. Отличие — шлагбаум принимает и параметры комитета."""
    from ops import hunter_seal as HS, hunter_court as HC
    import orchestrator.universe_resolver as U
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"hunter_push": True})
    monkeypatch.setattr(HS, "MAX_SEAL_LAG_DAYS", 10 ** 6)
    import importlib
    for n in ("ops.hunter_seal", "hunter_seal"):                 # у каждого бота теста — свой журнал печати
        monkeypatch.setattr(importlib.import_module(n), "PRED", tmp_path / "predictions.jsonl")
        monkeypatch.setattr(importlib.import_module(n), "OUTC", tmp_path / "outcomes.jsonl")
    last = (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)).isoformat()
    closes = [39.57 * (1.03 if i % 2 else 0.97) for i in range(60)] + [39.57]
    prices = {"last_close": 39.57, "last_date": last, "trigger_close": CARD["trigger_close"], "closes": closes}
    monkeypatch.setattr(HS, "_server_prices", lambda a, td=None, con=None: dict(prices, trigger_date=td))
    monkeypatch.setattr(U, "is_sealable", lambda a, **k: True)
    import idea_gate as G
    from ops import idea_gate as G2
    for mod in (G, G2):
        orig = mod.check
        monkeypatch.setattr(mod, "check", lambda c, today=None, horizon_from=None, _o=orig, **k:
                            _o(c, today=DAY, horizon_from=None, **k))
    courts = []
    monkeypatch.setattr(HC, "judge_card", lambda card, **k: (courts.append(card) or {
        "verdict": verdict, "objection": "проверить спрос", "evidence": "«x»"}, None))
    d = tmp_path / "reports" / "ideas"
    d.mkdir(parents=True)
    for name, text in files:
        (d / name).write_text(text, encoding="utf-8")
        (d / name.replace(".push.txt", ".md")).write_text(MD, encoding="utf-8")
        (d / name.replace(".push.txt", ".card.json")).write_text(_json.dumps(CARD, ensure_ascii=False),
                                                                 encoding="utf-8")
    if flag is not False:
        _flag(tmp_path, flag)
    b = _bot()
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
    monkeypatch.setattr(b, "_notice_owner", lambda t: notes.append(t))
    return b, notes


def _pin_day(monkeypatch, calls=None):
    """Вердикт считается настоящим кодом, но на дату карточки (бот передаёт сегодняшний день сервера)."""
    for m in _iv_mods():
        orig = m.verdict_for_card

        def _v(card, today=None, rules=None, court=None, _o=orig, **k):
            if calls is not None:
                calls.append({"court": court, **k})
            return _o(card, DAY, rules, court, **k)
        monkeypatch.setattr(m, "verdict_for_card", _v)


def _fake_verdict(monkeypatch, v, calls=None):
    for m in _iv_mods():
        monkeypatch.setattr(m, "verdict_for_card", lambda card, today=None, rules=None, court=None, **k:
                            ((calls.append({"court": court, **k}) if calls is not None else None), dict(v))[1])


def _v(score=4, decision="архив", missing=6, brak=(), ind=None):
    return {"когда": "2026-11-02", "источник": "идея охотника от 2026-11-01", "тикер": "MP.US", "имя": "MP Materials",
            "направление": "вверх", "цена": 39.57, "дата_цены": "2026-11-01", "цель": 51.441, "стоп": 35.613,
            "балл": score, "из": 10, "решение": decision,
            "решение_словами": {"архив": "архив", "ожидание": "лист ожидания",
                                "ставка": "кандидат на маленькую ставку"}[decision],
            "пункты": [{"код": "ПОЧЕМУ", "зачтён": True}],
            "не_хватает": [{"почему": f"не показано доказательство номер {i}", "словами": "пункт"}
                           for i in range(1, missing + 1)],
            "до_ставки": max(0, 8 - score), "брак": list(brak), "пороги": {"ставка": 8, "ожидание": 6},
            "независимость": {"словами": ind} if ind else None, "версия_листа": 1}


def _old_message(b):
    """Сообщение идеи по ПРЕЖНЕЙ формуле сборки: текст до рамки, пустая строка, строки печати, рамка."""
    (foot,) = b.state["hunter_footer"].values()
    lines = foot["text"].split("\n")
    at = max(i for i, ln in enumerate(lines) if PL.RESEARCH_FRAME in ln)
    return "\n".join(lines[:at] + [""] + list(foot["lines"]) + lines[at:])


def _verdict_files(tmp_path):
    return list(tmp_path.rglob("verdicts.jsonl"))


# ── флаг ─────────────────────────────────────────────────────────────────────────────────────────────
def test_flag_is_read_from_presentation_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    assert BOT.hunter_committee_on() is False                    # файла нет
    _flag(tmp_path, None)
    assert BOT.hunter_committee_on() is False                    # ключа нет
    _flag(tmp_path, False)
    assert BOT.hunter_committee_on() is False
    _flag(tmp_path, True)
    assert BOT.hunter_committee_on() is True
    (tmp_path / "config" / "presentation.yaml").write_text("hunter_committee_push: [true\n", encoding="utf-8")
    assert BOT.hunter_committee_on() is False                    # файл не читается — выключено
    (tmp_path / "config" / "presentation.yaml").write_text("- просто\n- список\n", encoding="utf-8")
    assert BOT.hunter_committee_on() is False


def test_flag_is_off_in_repo_config():
    """В настройках репозитория флага нет (его впишет интегратор после «ок» владельца) — значит выключен."""
    assert BOT.hunter_committee_on(ROOT) is False


# ── выключенный флаг: байт в байт ───────────────────────────────────────────────────────────────────
def test_flag_off_message_is_byte_identical_and_committee_is_not_touched(tmp_path, monkeypatch):
    calls = []
    for m in _iv_mods():
        monkeypatch.setattr(m, "verdict_for_card", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
            AssertionError("при выключенном флаге комитет не считается")))
    for flag in (False, None, "false"):                          # нет файла настроек / нет ключа / выключен явно
        root = tmp_path / str(flag)
        root.mkdir()
        b, _notes = _setup(root, monkeypatch, flag=flag)
        b._tick_hunter_ideas()
        assert len(b.tg.sent) == 1
        _chat, text, kb = b.tg.sent[0]
        assert text == _old_message(b)                           # прежняя формула сборки — байт в байт
        assert "Комитет" not in text and IV.DISCLAIMER not in text
        (foot,) = b.state["hunter_footer"].values()
        assert set(foot) == OLD_FOOT_KEYS                        # в состоянии бота — ни одного нового ключа
        assert kb is not None and not _verdict_files(root) and not calls


def test_flag_on_differs_from_flag_off_only_by_the_block(tmp_path, monkeypatch):
    """Та же идея при включённом флаге = прежнее сообщение + блок кода; вырезали блок — получили прежнее."""
    off_root, on_root = tmp_path / "off", tmp_path / "on"
    off_root.mkdir(), on_root.mkdir()
    b0, _ = _setup(off_root, monkeypatch, flag=False)
    b0._tick_hunter_ideas()
    off_text = b0.tg.sent[0][1]
    _pin_day(monkeypatch)
    b1, _ = _setup(on_root, monkeypatch, flag=True)
    b1._tick_hunter_ideas()
    on_text = b1.tg.sent[0][1]
    (foot,) = b1.state["hunter_footer"].values()
    block = IV.fit_short(foot["committee"])
    assert block and block in on_text and on_text != off_text
    assert on_text.replace("\n\n" + block + "\n", "\n", 1) == off_text
    assert b0.tg.sent[0][2] == b1.tg.sent[0][2]                  # кнопки те же


# ── включённый флаг ─────────────────────────────────────────────────────────────────────────────────
def test_block_is_added_by_code_with_court_verdict_and_saved_once(tmp_path, monkeypatch):
    calls = []
    _pin_day(monkeypatch, calls)
    b, _notes = _setup(tmp_path, monkeypatch, verdict="PASS")
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1
    text = b.tg.sent[0][1]
    want = IV.verdict_for_card(CARD, DAY, None, {"verdict": "PASS"}, sector_fn=False, independence_fn=False)
    lines = text.split("\n")
    head = next(i for i, ln in enumerate(lines) if ln.startswith("Комитет: "))
    assert lines[head] == f"Комитет: {want['балл']} из 10 — {want['решение_словами']}."
    assert lines[head - 1] == ""                                 # блок отделён пустой строкой от строк печати
    miss = [ln for ln in lines if ln.startswith("не хватает: ")]
    assert 1 <= len(miss) <= 3
    assert miss[0] == "не хватает: " + (want["не_хватает"][0]["почему"] or want["не_хватает"][0]["словами"])
    # оговорка «балл — не вероятность» — последней строкой блока, сразу перед рамкой «не рекомендация»
    frame = max(i for i, ln in enumerate(lines) if PL.RESEARCH_FRAME in ln)
    assert lines[frame - 1] == IV.DISCLAIMER and head < frame
    assert "Записал в журнал проверку" in "\n".join(lines[:head])   # строки печати на месте, выше блока
    assert calls and calls[0]["court"] == {"verdict": "PASS"}    # вердикт суда — в пункт 10
    assert any(p["код"] == "СУД" and p["зачтён"] is True for p in want["пункты"])
    # запись ДО исхода — одна; повторный тик ничего не шлёт и не пишет
    (vf,) = _verdict_files(tmp_path)
    recs = [_json.loads(x) for x in vf.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 1 and recs[0]["балл"] == want["балл"] and recs[0]["тикер"] == "MP.US"
    assert recs[0]["источник"] == "идея охотника от 2026-11-01" and recs[0]["когда"] == DAY.isoformat()
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and len(vf.read_text(encoding="utf-8").splitlines()) == 1


def test_block_is_not_linted_as_model_words(tmp_path, monkeypatch):
    """Блок пишет код: слова и числа, за которые текст МОДЕЛИ был бы забракован, в блоке идею не задерживают."""
    ind = ("Независимость: частичный повтор — за 120 торговых дней ходила вместе с BRNT на 55%. Вместе с ней "
           "открыто было бы 9 ставок — по сути это 3.2 независимых.")
    _fake_verdict(monkeypatch, _v(score=7, decision="ожидание", missing=3, brak=["шанс 65% не подтверждён"], ind=ind))
    b, notes = _setup(tmp_path, monkeypatch)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and not notes
    text = b.tg.sent[0][1]
    assert "Комитет: 7 из 10 — лист ожидания." in text and ind in text
    assert "мешает ставке: шанс 65% не подтверждён" in text
    assert PL.lint_hunter_idea(text), "такой текст от модели линт бы не пропустил — блок в линт не идёт"


def test_committee_failure_does_not_hold_the_idea(tmp_path, monkeypatch):
    for m in _iv_mods():
        monkeypatch.setattr(m, "verdict_for_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет цен")))
    b, notes = _setup(tmp_path, monkeypatch)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and not notes
    assert b.tg.sent[0][1] == _old_message(b) and b.tg.sent[0][2] is not None
    assert not _verdict_files(tmp_path)
    assert NAME in {k.split("@")[0] for k in b.state["hunter_delivered"]}


def test_save_failure_keeps_block_and_delivery(tmp_path, monkeypatch):
    _fake_verdict(monkeypatch, _v())
    for m in _iv_mods():
        monkeypatch.setattr(m, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("диск")))
    b, _notes = _setup(tmp_path, monkeypatch)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and "Комитет: 4 из 10 — архив." in b.tg.sent[0][1]


def test_updated_version_carries_block_but_writes_no_duplicate(tmp_path, monkeypatch):
    calls = []
    _fake_verdict(monkeypatch, _v(), calls)
    b, _notes = _setup(tmp_path, monkeypatch)
    b._tick_hunter_ideas()
    f = tmp_path / "reports" / "ideas" / NAME
    f.write_text(GOOD.replace("Что делать.", "Что изменилось. Отчёт вышел — план прежний.\n\nЧто делать."),
                 encoding="utf-8")
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 2
    upd = b.tg.sent[1][1]
    assert upd.startswith("Обновлённая версия записки.") and "Комитет: 4 из 10 — архив." in upd
    assert len(calls) == 2                                       # вердикт пересчитан для новой версии…
    (vf,) = _verdict_files(tmp_path)
    assert len(vf.read_text(encoding="utf-8").splitlines()) == 1  # …а запись ДО исхода — одна на идею


def test_court_against_idea_is_still_shown_with_score(tmp_path, monkeypatch):
    """Балл — пометка, а не ворота: идея с доводом суда против показывается «для обсуждения» вместе с баллом."""
    calls = []
    _pin_day(monkeypatch, calls)
    b, _notes = _setup(tmp_path, monkeypatch, verdict="FATAL")
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1
    text = b.tg.sent[0][1]
    assert "показываю для обсуждения" in text and "проверочная идея" in text
    assert calls[0]["court"] == {"verdict": "FATAL"}
    want = IV.verdict_for_card(CARD, DAY, None, {"verdict": "FATAL"}, sector_fn=False, independence_fn=False)
    assert f"Комитет: {want['балл']} из 10 — {want['решение_словами']}." in text
    assert any(p["код"] == "СУД" and p["зачтён"] is False for p in want["пункты"])
    lines = text.split("\n")
    assert lines[max(i for i, ln in enumerate(lines) if PL.RESEARCH_FRAME in ln) - 1] == IV.DISCLAIMER
    (vf,) = _verdict_files(tmp_path)
    assert len(vf.read_text(encoding="utf-8").splitlines()) == 1
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and len(vf.read_text(encoding="utf-8").splitlines()) == 1


def test_court_against_with_flag_off_is_unchanged(tmp_path, monkeypatch):
    for m in _iv_mods():
        monkeypatch.setattr(m, "verdict_for_card", lambda *a, **k: (_ for _ in ()).throw(AssertionError("выключено")))
    b, _notes = _setup(tmp_path, monkeypatch, verdict="FATAL", flag=False)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and "Комитет" not in b.tg.sent[0][1] and not _verdict_files(tmp_path)


def test_flag_switched_off_after_verdict_was_cached(tmp_path, monkeypatch):
    """Вердикт посчитан, а отправка сорвалась (сеть); до повтора флаг выключили — уходит прежнее сообщение."""
    _fake_verdict(monkeypatch, _v())
    b, _notes = _setup(tmp_path, monkeypatch)
    real_send = b.tg.send_message
    monkeypatch.setattr(b.tg, "send_message", lambda *a, **k: None)
    b._tick_hunter_ideas()
    assert not b.tg.sent and "committee" in next(iter(b.state["hunter_footer"].values()))
    _flag(tmp_path, False)
    monkeypatch.setattr(b.tg, "send_message", real_send)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1 and b.tg.sent[0][1] == _old_message(b)


# ── лимит Telegram ──────────────────────────────────────────────────────────────────────────────────
def _attach(tmp_path, monkeypatch, base_len, v):
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    _flag(tmp_path, True)
    b = _bot()
    tail = "\n" + PL.RESEARCH_FRAME
    out = "Идея.\n" + "я" * (base_len - len("Идея.\n") - len(tail)) + tail
    assert len(out) == base_len
    return out, b._committee_attach(out, IV.short_lines(v), PL.RESEARCH_FRAME)


def test_block_shrinks_to_fit_telegram_limit(tmp_path, monkeypatch):
    ind = "Независимость: новая ставка — с ближайшей открытой (BRNT) связь 12%. " + "Длинное пояснение. " * 12
    v = _v(score=7, decision="ожидание", missing=6, brak=["нет правила выхода"], ind=ind)
    full = IV.render_short(v)
    assert full.count("не хватает: ") == 3 and "ещё не зачтено пунктов: 3." in full and "мешает ставке: " in full
    limit = CH.MAX_REPLY_CHUNK
    # места много — блок целиком, перед рамкой
    out, got = _attach(tmp_path, monkeypatch, 1000, v)
    assert got == out.replace("\n" + PL.RESEARCH_FRAME, "\n\n" + full + "\n" + PL.RESEARCH_FRAME)
    # места мало — блок сокращён, но итог и оговорка на месте; сообщение — одной частью
    out, got = _attach(tmp_path, monkeypatch, limit - 260, v)
    assert len(got) <= limit and got.startswith(out[:200])
    assert "Комитет: 7 из 10 — лист ожидания." in got and IV.DISCLAIMER in got
    assert got.count("не хватает: ") < 3 and "Длинное пояснение" not in got
    # места нет совсем — идея уходит как была
    out, got = _attach(tmp_path, monkeypatch, limit - 60, v)
    assert got == out
    # сообщение и так шло двумя частями — третьей из-за блока не появляется
    out, got = _attach(tmp_path, monkeypatch, 2 * limit - 150, v)
    assert len(got) <= 2 * limit and (got == out or "Комитет: 7 из 10" in got)


def test_long_idea_is_delivered_in_one_message_with_shortened_block(tmp_path, monkeypatch):
    """Через весь тик: запись на пределе длины уходит одним сообщением, идея цела, блок короче полного."""
    ind = "Независимость: новая ставка — с ближайшей открытой (BRNT) связь 12%. " + "Длинное пояснение. " * 12
    v = _v(score=7, decision="ожидание", missing=6, ind=ind)
    _fake_verdict(monkeypatch, v)
    (tmp_path / "off").mkdir(), (tmp_path / "on").mkdir()
    b0, _ = _setup(tmp_path / "off", monkeypatch, flag=False)
    b0._tick_hunter_ideas()
    limit = len(b0.tg.sent[0][1]) + 250                          # места — только на сокращённый блок
    monkeypatch.setattr(CH, "MAX_REPLY_CHUNK", limit)
    b, _notes = _setup(tmp_path / "on", monkeypatch)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1
    text = b.tg.sent[0][1]
    old = _old_message(b)
    assert old == b0.tg.sent[0][1] and len(text) <= limit
    assert "Комитет: 7 из 10 — лист ожидания." in text and IV.DISCLAIMER in text
    assert len(IV.render_short(v)) + len(old) > limit            # полный блок не влез бы
    assert all(ln in text.split("\n") for ln in old.split("\n"))  # ни одна строка идеи не потеряна


# ── короткий рендер ─────────────────────────────────────────────────────────────────────────────────
def test_render_short_format():
    v = _v(score=4, decision="архив", missing=6, brak=["прогноз нельзя честно сверить"])
    assert IV.render_short(v).split("\n") == [
        "Комитет: 4 из 10 — архив.",
        "не хватает: не показано доказательство номер 1",
        "не хватает: не показано доказательство номер 2",
        "не хватает: не показано доказательство номер 3",
        "ещё не зачтено пунктов: 3.",
        IV.DISCLAIMER]                                           # в архиве строка «мешает ставке» лишняя
    v = _v(score=9, decision="ожидание", missing=1, brak=["нет правила выхода", "второй"],
           ind="Независимость: открытых ставок нет — это была бы первая.")
    assert IV.render_short(v).split("\n") == [
        "Комитет: 9 из 10 — лист ожидания.",
        "не хватает: не показано доказательство номер 1",
        "мешает ставке: нет правила выхода (и ещё причин: 1)",
        "Независимость: открытых ставок нет — это была бы первая.",
        IV.DISCLAIMER]
    v = _v(score=10, decision="ставка", missing=0)
    assert IV.render_short(v).split("\n") == ["Комитет: 10 из 10 — кандидат на маленькую ставку.", IV.DISCLAIMER]


def test_render_short_long_reason_is_cut_at_word_boundary():
    v = _v(missing=1)
    v["не_хватает"][0]["почему"] = "слово " * 80
    line = IV.render_short(v).split("\n")[1]
    assert len(line) <= len("не хватает: ") + IV.SHORT_LINE + 1 and line.endswith("слово…")


def test_fit_short_never_returns_score_without_disclaimer():
    lines = IV.short_lines(_v(score=7, decision="ожидание", missing=5, brak=["нет правила выхода"],
                              ind="Независимость: новая ставка. Вторая фраза про число ставок."))
    full = IV.fit_short(lines)
    seen = set()
    for room in range(len(full), 0, -1):
        got = IV.fit_short(lines, room)
        assert len(got) <= room
        if got:
            assert got.startswith("Комитет: 7 из 10 — лист ожидания.") and got.endswith(IV.DISCLAIMER)
        seen.add(got)
    assert "" in seen and len(seen) > 4                          # сокращается по шагам, а не сразу исчезает
    assert IV.fit_short([]) == "" and IV.fit_short(None, 100) == ""


# ── независимость ставок: идея не сравнивается сама с собой ─────────────────────────────────────────
def test_open_bets_can_skip_the_idea_own_sealed_record(monkeypatch):
    """Идея охотника печатается ДО доставки — её собственная запись в журнале прогнозов не «открытая ставка»."""
    monkeypatch.undo()                                           # нужна настоящая open_bets; счёт передан явно
    own, other = "a" * 64, "b" * 64
    ledger = {"позиции": [], "предложения": [
        {"тикер": "DHT.US", "direction": "above", "дата": "2026-11-03", "срок": "2026-12-15",
         "источник": f"журнал прогнозов hunter_idea {own[:12]}"},
        {"тикер": "MP.US", "direction": "above", "дата": "2026-11-01", "срок": "2026-12-15",
         "источник": f"журнал прогнозов hunter_idea {other[:12]}"},
        {"тикер": "BRNT.LSE", "direction": "above", "дата": "2026-11-10", "источник": "разговор 10.11"}]}
    day = _dt.date(2026, 11, 3)
    assert [b["тикер"] for b in IV.open_bets(day, ledger)] == ["DHT.US", "MP.US", "BRNT.LSE"]      # как раньше
    assert [b["тикер"] for b in IV.open_bets(day, ledger, skip=(own,))] == ["MP.US", "BRNT.LSE"]
    assert [b["тикер"] for b in IV.open_bets(day, ledger, skip=(None, ""))] == ["DHT.US", "MP.US", "BRNT.LSE"]


def test_ledger_names_sealed_proposals_by_hash_prefix(tmp_path):
    """Пропуск собственной записи держится на том, как общий счёт подписывает предложения из журнала прогнозов."""
    import ledger as LG
    pred = tmp_path / "predictions.jsonl"
    rec = {"kind": "hunter_idea", "asset": "DHT.US", "direction": "above", "якорь_дата": "2026-11-03",
           "якорь_цена": 23.86, "threshold": 27.0, "resolve_by": "2026-12-15", "hash": "c" * 64}
    pred.write_text(_json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    (row,) = LG.proposals(pred, tmp_path / "нет.jsonl", tmp_path / "нет2.jsonl", tmp_path / "нет_папки")
    assert row["источник"] == "журнал прогнозов hunter_idea " + "c" * 12


def test_bot_excludes_own_sealed_record_from_open_bets(tmp_path, monkeypatch):
    from ops import hunter_seal as HS
    _pin_day(monkeypatch)
    seen = []
    for m in _iv_mods():
        monkeypatch.setattr(m, "open_bets", lambda today=None, ledger=None, skip=(): seen.append(tuple(skip)) or [])
    b, _notes = _setup(tmp_path, monkeypatch)
    b._tick_hunter_ideas()
    assert len(b.tg.sent) == 1
    own = HS.sealed_record(NAME)
    assert own and seen == [(own["hash"],)]
    assert "Независимость: открытых ставок нет — это была бы первая." in b.tg.sent[0][1].split("\n")


def test_short_block_for_real_hunter_card():
    """Настоящая карточка охотника (DHT Holdings, 03.11, суд PASS): блок собирается, строк не больше семи."""
    card = _json.loads((ROOT / "reports" / "ideas" / "idea_2026-11-03_DHT.card.json").read_text(encoding="utf-8"))
    v = IV.verdict_for_card(card, _dt.date(2026, 11, 3), None, {"verdict": "PASS"}, sector_fn=False,
                            independence_fn=False)
    lines = IV.render_short(v).split("\n")
    assert lines[0] == f"Комитет: {v['балл']} из {v['из']} — {v['решение_словами']}." and lines[-1] == IV.DISCLAIMER
    assert sum(ln.startswith("не хватает: ") for ln in lines) == min(3, len(v["не_хватает"])) and len(lines) <= 7
    assert IV.render(v).startswith("Вердикт по DHT Holdings (DHT)")   # прежний полный рендер не тронут
