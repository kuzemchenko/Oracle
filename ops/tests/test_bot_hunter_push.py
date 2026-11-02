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
            (d / name.replace(".push.txt", ".md")).write_text("# источники: URL+даты",
                                                              encoding="utf-8")
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
    """Перепроверка №2 02.11 (П8): пуш без файла исследования с источниками рядом — брак."""
    b, notes = _setup(tmp_path, monkeypatch, [("idea_2026-11-04_X.push.txt", GOOD)],
                      no_md=("idea_2026-11-04_X.push.txt",))
    b._tick_hunter_ideas()
    assert not b.tg.sent and notes and "источник" in notes[0]


def test_odds_wordforms_blocked():
    """Перепроверка №2 02.11: обходы запрета прописью закрыты; величины хода — живут."""
    for bad in ("Выплата один к четырём.", "Сбывается в девяти случаях из десяти.",
                "Вероятно, продлят паузу.", "Срыв в трети случаев.", "Шансы невелики."):
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
