# -*- coding: utf-8 -*-
"""Тик доставки идей охотника (вопрос владельца 02.11: «MP так и не попала в Telegram»).
Закрепляет: новый reports/ideas/*.push.txt уходит в Telegram один раз (дедуп по имени);
битый текст — не уходит, заметка в дозор; сбой сети — ретрай следующим тиком."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import bot as BOT                    # noqa: E402
from test_bot_v2_loop import _bot    # noqa: E402

GOOD = ("Наблюдение с триггером — проверочная идея.\n\n"
        "Что это значит для тебя. Ничего делать не нужно.\n\n"
        "Что делать. Ждать. Решение за тобой.\n"
        "Это исследовательский инструмент, не инвестиционная рекомендация.")


def test_hunter_push_delivers_once_and_lints(tmp_path, monkeypatch):
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    d = tmp_path / "reports" / "ideas"
    d.mkdir(parents=True)
    (d / "idea_2026-11-01_MP.push.txt").write_text(GOOD, encoding="utf-8")
    (d / "idea_2026-11-02_BAD.push.txt").write_text("Жаргон: каскад и edge 🚀", encoding="utf-8")
    b = _bot()
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
    b._tick_hunter_ideas()
    sent = [t for _c, t, _k in b.tg.sent]
    assert any("проверочная идея" in t for t in sent)
    assert not any("каскад" in t for t in sent), "битый текст не должен уходить"
    assert notes and "BAD" in notes[0]
    n = len(b.tg.sent)
    b._tick_hunter_ideas()                       # повтор — дедуп
    assert len(b.tg.sent) == n
    assert sorted(b.state["hunter_pushed"]) == ["idea_2026-11-01_MP.push.txt",
                                                "idea_2026-11-02_BAD.push.txt"]
