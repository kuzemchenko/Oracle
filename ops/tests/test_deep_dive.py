# -*- coding: utf-8 -*-
"""Тесты V3 «качаем 2 дня» (orchestrator/deep_dive.py). Герметично: challenge — стаб,
очередь/заметки/паспорта — tmp, сети и LLM нет.

Закрепляют:
  • кнопка (queued) → день-1: challenge с сильнейшим возражением, текст через линт → notices,
    стадия day1 дописана append-only;
  • день-2 НЕ в тот же день (день-1 сегодня → пропуск с причиной); назавтра → паспорт .md +
    экономика §11 (фикс микро-размер $500, Келли честно выключен) → notices, стадия day2;
  • завершённая идея (day2) не трогается повторно; ОТКАЗ challenge — ошибка одной идеи,
    не всего прогона; сломанный текст (линт) владельцу НЕ уходит.
"""
import json
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from orchestrator import deep_dive as DD     # noqa: E402

D1 = datetime.datetime(2026, 11, 2, 9, 40, tzinfo=datetime.timezone.utc)
D2 = datetime.datetime(2026, 11, 3, 9, 40, tzinfo=datetime.timezone.utc)


def _queue(tmp_path, asset="LNG.US"):
    q = tmp_path / "deep_dive.jsonl"
    q.write_text(json.dumps({"ts": "2026-11-01T18:00:00+00:00", "run_id": "ef_x",
                             "asset": asset, "status": "queued",
                             "source": "button_deep_dive"}, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return q


def _stub_challenge(asset):
    return {"run_id": "challenge_test_1", "вердикт": "УСТОЯЛА С ОГОВОРКАМИ",
            "резюме": f"Разбор по {asset}: сильнейшее возражение — спрос уже в цене; идея "
                      f"держится на узком горле поставок, но связь коротка.",
            "сценарии_инвалидации": ["цена фрахта откатится к средней за месяц",
                                     "узкое горло расшивается новыми маршрутами"]}


def test_day1_then_day2_full_cycle(tmp_path):
    q = _queue(tmp_path)
    notices = tmp_path / "notices.jsonl"
    r1 = DD.run_deep_dive(now=D1, queue_path=q, notices_path=notices,
                          passports_dir=tmp_path / "pass", challenge_fn=_stub_challenge)
    assert r1["день1"] == 1 and r1["день2"] == 0 and not r1["ошибки"]
    notes = [json.loads(x) for x in notices.read_text("utf-8").splitlines()]
    assert "день первый" in notes[0]["text"] and "Идея умирает, если" in notes[0]["text"]
    assert "Cheniere" in notes[0]["text"]                       # тикер с именем
    # тот же день: день-2 не запускается
    r_same = DD.run_deep_dive(now=D1, queue_path=q, notices_path=notices,
                              passports_dir=tmp_path / "pass", challenge_fn=_stub_challenge)
    assert r_same["продвинуто"] == 0 and r_same["пропущено"]
    # назавтра: день-2 → паспорт + экономика
    r2 = DD.run_deep_dive(now=D2, queue_path=q, notices_path=notices,
                          passports_dir=tmp_path / "pass", challenge_fn=_stub_challenge)
    assert r2["день2"] == 1 and not r2["ошибки"]
    notes = [json.loads(x) for x in notices.read_text("utf-8").splitlines()]
    day2 = notes[-1]["text"]
    assert "день второй" in day2 and "$500" in day2 and "Келли" in day2
    assert "цена фрахта откатится" in day2                      # смерть идеи из дня-1
    passports = list((tmp_path / "pass").glob("*.md"))
    assert len(passports) == 1 and "Паспорт идеи" in passports[0].read_text("utf-8")
    # завершённая идея больше не трогается
    r3 = DD.run_deep_dive(now=D2 + datetime.timedelta(days=1), queue_path=q,
                          notices_path=notices, passports_dir=tmp_path / "pass",
                          challenge_fn=_stub_challenge)
    assert r3["продвинуто"] == 0


def test_challenge_refusal_is_per_idea(tmp_path):
    q = _queue(tmp_path, asset="GEV.US")
    r = DD.run_deep_dive(now=D1, queue_path=q, notices_path=tmp_path / "n.jsonl",
                         passports_dir=tmp_path / "pass",
                         challenge_fn=lambda a: {"ОТКАЗ": "бюджет месяца исчерпан"})
    assert r["день1"] == 0 and r["ошибки"] and "бюджет" in r["ошибки"][0]["ошибка"]
    assert not (tmp_path / "n.jsonl").exists(), "при отказе владельцу ничего не шлём"


def test_lint_blocks_broken_text(tmp_path, monkeypatch):
    q = _queue(tmp_path)
    monkeypatch.setattr(DD, "_lint_or_fallback", lambda text: ["сломанный шаблон"])
    r = DD.run_deep_dive(now=D1, queue_path=q, notices_path=tmp_path / "n.jsonl",
                         passports_dir=tmp_path / "pass", challenge_fn=_stub_challenge)
    assert r["день1"] == 0 and r["ошибки"] and "линт" in r["ошибки"][0]["ошибка"]
    assert not (tmp_path / "n.jsonl").exists()
