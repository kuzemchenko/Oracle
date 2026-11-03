# -*- coding: utf-8 -*-
"""Тесты V2 «предложения петли §25 с кнопками» (ops/bot_proposals.py + тик бота).

Закрепляют: очередь из свежайшего proposals_*.json с дедупом против решений;
карточка простыми словами (наблюдение/предложение/эффект) с кнопками ✅/❌;
решение кнопкой → journal/proposal_decisions.jsonl append-only (подпись §10 —
вносит машинист, бот конфиги не трогает); недельный дедуп (воскресенье);
устаревший callback отбивается. Герметично: tmp, фейковый Telegram, сети нет.
"""
import json
import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import bot_proposals as PR    # noqa: E402

from test_bot_v2_loop import FakeTelegram, _bot    # noqa: E402


def _props_file(tmp_path, n=3, ts="2026-10-30T18:00:01+00:00", name="proposals_20261030T180001.json"):
    d = tmp_path / "proposals"
    d.mkdir(exist_ok=True)
    doc = {"тип": "devops-предложения §R6", "ts": ts,
           "предложения": [{"наблюдение": f"ворота номер {i} валят половину отсева",
                            "предложение": f"пересмотреть ворота номер {i}",
                            "эффект": "шире вход, но больше шума",
                            "требует_подписи": True} for i in range(n)]}
    (d / name).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return d


def test_pending_latest_file_and_dedup(tmp_path):
    d = _props_file(tmp_path, n=2, ts="2026-10-18T18:00:01+00:00",
                    name="proposals_20261018T180001.json")
    _props_file(tmp_path, n=3)                      # свежее — берётся оно
    dec = tmp_path / "dec.jsonl"
    items = PR.pending(d, dec)
    assert len(items) == 3
    # решение по первому → исчезает из очереди
    k, row = items[0]
    PR.record_decision(k, "signed", row, "555", decisions_path=dec)
    items2 = PR.pending(d, dec)
    assert len(items2) == 2 and all(kk != k for kk, _ in items2)
    rec = json.loads(dec.read_text("utf-8").splitlines()[0])
    assert rec["решение"] == "signed" and rec["файл_источник"].startswith("proposals_2026103")
    assert rec["требует_подписи"] is True


def test_card_plain_words_and_keyboard(tmp_path):
    d = _props_file(tmp_path)
    items = PR.pending(d, tmp_path / "dec.jsonl")
    card = PR.format_card(items)
    assert "Решение" in card and "всегда твоё" in card
    assert card.count("Что замечено:") == 3 and card.count("Что это даст:") == 3
    assert "машинист" in card and "Ничего само не меняется" in card
    kb = PR.build_keyboard(items)
    assert len(kb["inline_keyboard"]) == 3
    assert kb["inline_keyboard"][0][0]["callback_data"].startswith("p:y:")
    assert PR.format_card([]) is None


def test_tick_weekly_sunday_and_button_flow(tmp_path, monkeypatch):
    b = _bot()
    monkeypatch.setattr(PR, "PROP_DIR", _props_file(tmp_path))
    monkeypatch.setattr(PR, "DECISIONS", tmp_path / "dec.jsonl")
    sun = datetime.datetime(2026, 11, 8, 11, 0, tzinfo=datetime.timezone.utc)
    assert sun.isoweekday() == 7
    b._tick_proposal_batch(sun)
    assert b.tg.sent and b.tg.sent[-1][2]["inline_keyboard"], "карточка без кнопок"
    assert len(b.state["proposals_pending"]) == 3
    n = len(b.tg.sent)
    b._tick_proposal_batch(sun + datetime.timedelta(hours=2))    # та же неделя — дедуп
    assert len(b.tg.sent) == n
    # кнопка ✅ → запись signed + снятие из pending
    key = next(iter(b.state["proposals_pending"]))
    cb = {"id": "cb1", "data": f"p:y:{key}", "message": {"chat": {"id": 555}}}
    b._on_callback(cb)
    rec = json.loads((tmp_path / "dec.jsonl").read_text("utf-8").splitlines()[0])
    assert rec["решение"] == "signed"
    assert key not in b.state["proposals_pending"]
    # устаревший ключ отбивается вежливо
    b._on_callback({"id": "cb2", "data": f"p:y:{key}", "message": {"chat": {"id": 555}}})
    assert "устарела" in (b.tg.answers[-1][1] or "") or "разобрано" in (b.tg.answers[-1][1] or "")
    # суббота — карточка не шлётся (день тем)
    b2 = _bot()
    monkeypatch.setattr(PR, "PROP_DIR", _props_file(tmp_path))
    b2._tick_proposal_batch(datetime.datetime(2026, 11, 7, 11, 0, tzinfo=datetime.timezone.utc))
    assert not b2.tg.sent


def test_info_only_items_not_put_to_signature(tmp_path):
    """Аудит 03.11 (P1 №17): справочные пункты (требует_подписи=false) в карточку подписи не идут."""
    d = tmp_path / "proposals"
    d.mkdir()
    doc = {"ts": "2026-11-06T18:00:01+00:00", "предложения": [
        {"наблюдение": "фильтр старого конвейера", "предложение": "к сведению", "эффект": "—", "требует_подписи": False},
        {"наблюдение": "цепочка мертва", "предложение": "проверить цепочку", "эффект": "чистка", "требует_подписи": True}]}
    (d / "proposals_20261106T180001.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    items = PR.pending(d, tmp_path / "dec.jsonl")
    assert [r["item"]["наблюдение"] for _k, r in items] == ["цепочка мертва"]
