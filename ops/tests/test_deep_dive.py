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


def _battle_challenge(asset):
    """БОЕВАЯ форма протокола challenge.py: вердикт в дебаты.вердикт.исход, аргументы критика
    в дебаты.реплики.критик.judgment, резюме с эмодзи (как вживую)."""
    return {"run_id": "challenge_battle_1",
            "резюме": f"Разбор идеи: {asset} (лонг)\n\n🟢 Защита (генератор): каскад опирается на "
                      f"событийный якорь.\n🔴 Критика: связь не подтверждена.",
            "дебаты": {"вердикт": {"исход": "РАЗБИТА", "средний_балл_рубрики": 2.0, "порог": 3.0},
                       "реплики": {"критик": {"judgment": {
                           "вывод": "Лонг по данному каскаду не держится: механизм переноса "
                                    "события на бумагу не обоснован, надёжность связи низкая.",
                           "сильнейшее_возражение": "исходное движение не подтверждено котировками",
                           "скрытые_допущения": ["лаг переноса совпадёт с историческим",
                                                 "объём события достаточен для второго звена"],
                       }}}}}


def test_day1_battle_protocol_shape(tmp_path):
    """Аудит 01.11: вживую ключи лежат в дебатах, резюме суда — с эмодзи. День-1 обязан
    собрать «Идея умирает, если» из критика и пройти линт (эмодзи вычищены)."""
    q = _queue(tmp_path)
    notices = tmp_path / "notices.jsonl"
    r = DD.run_deep_dive(now=D1, queue_path=q, notices_path=notices,
                         passports_dir=tmp_path / "pass", challenge_fn=_battle_challenge)
    assert r["день1"] == 1 and not r["ошибки"], r["ошибки"]
    note = json.loads(notices.read_text("utf-8").splitlines()[0])["text"]
    assert "Идея умирает, если" in note
    assert "сильнейшее возражение критика" in note
    assert "🟢" not in note and "🔴" not in note
    # резюме в очереди — чистый вывод критика, не эмодзи-простыня
    rows = [json.loads(x) for x in (q.read_text("utf-8")).splitlines()]
    day1 = [x for x in rows if x.get("stage") == "day1"][0]
    assert "не держится" in day1["резюме"] and "🟢" not in day1["резюме"]
    assert len(day1["смерть"]) == 3           # возражение + 2 допущения
    # день-2 печатает смерть идеи из боевого дня-1
    r2 = DD.run_deep_dive(now=D2, queue_path=q, notices_path=notices,
                          passports_dir=tmp_path / "pass", challenge_fn=_battle_challenge)
    assert r2["день2"] == 1 and not r2["ошибки"]
    day2 = json.loads(notices.read_text("utf-8").splitlines()[-1])["text"]
    assert "сильнейшее возражение критика" in day2


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


# ── аудит 03.11: «Качаем» по идее охотника прокачивает ЕЁ карточку, а не старую воронку ──────
def _hunter_setup(tmp_path, with_card=True):
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    src = ROOT / "reports" / "ideas" / "idea_2026-11-02_MP.card.json"
    if with_card:
        (ideas / "idea_2026-11-02_MP.card.json").write_text(src.read_text("utf-8"), encoding="utf-8")
    q = tmp_path / "deep_dive.jsonl"
    rows = [{"ts": "2026-10-31T08:00:00+00:00", "run_id": "ef_old", "asset": "LNG.US", "status": "queued"},
            {"ts": "2026-11-01T07:40:00+00:00", "run_id": "h_2026-11-02_MP", "asset": "MP.US",
             "status": "queued", "source": "button_deep_dive"}]
    q.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return ideas, q


def test_hunter_idea_day1_uses_card_candidate_and_goes_first(tmp_path):
    ideas, q = _hunter_setup(tmp_path)
    seen = []

    def ch(asset, candidate=None):
        seen.append((asset, candidate))
        return _stub_challenge(asset)
    r = DD.run_deep_dive(now=D1, queue_path=q, notices_path=tmp_path / "n.jsonl",
                         passports_dir=tmp_path / "pass", challenge_fn=ch, max_per_run=1, ideas_dir=ideas)
    assert r["день1"] == 1 and not r["ошибки"]
    asset, cand = seen[0]
    assert asset == "MP.US" and cand["актив"] == "MP.US" and cand["направление"] == "лонг"
    assert "47.5" in cand["разрешимость"] and "2026-11-25" in cand["разрешимость"]
    assert "охотник" in cand["школа"]


def test_hunter_idea_without_card_refuses_without_funnel_fallback(tmp_path):
    ideas, q = _hunter_setup(tmp_path, with_card=False)
    calls = []
    r = DD.run_deep_dive(now=D1, queue_path=q, notices_path=tmp_path / "n.jsonl",
                         passports_dir=tmp_path / "pass", ideas_dir=ideas,
                         challenge_fn=lambda a, candidate=None: calls.append(a) or _stub_challenge(a))
    assert any("нет карточки" in e["ошибка"] for e in r["ошибки"])
    assert calls == ["LNG.US"]                       # MP не ушёл в старую воронку
    assert "карточки" in (tmp_path / "n.jsonl").read_text("utf-8")


def test_hunter_day2_plan_from_card_prices(tmp_path):
    ideas, q = _hunter_setup(tmp_path)
    kw = dict(queue_path=q, notices_path=tmp_path / "n.jsonl", passports_dir=tmp_path / "pass",
              challenge_fn=lambda a, candidate=None: _stub_challenge(a), ideas_dir=ideas)
    DD.run_deep_dive(now=D1, **kw)
    r2 = DD.run_deep_dive(now=D2, **kw)
    assert r2["день2"] >= 1 and not r2["ошибки"]
    texts = [json.loads(x)["text"] for x in (tmp_path / "n.jsonl").read_text("utf-8").splitlines()]
    mp = [t for t in texts if "день второй" in t and "MP" in t][0]
    for need in ("$40.30", "$47.50", "$35.50", "25 ноября", "$500"):
        assert need in mp, need
    assert "разбери" not in mp                       # больше не обещаем «разбери» = паспорт
    passport = (tmp_path / "pass" / "MP_US_20261103.md").read_text("utf-8")
    assert "$47.50" in passport


def test_show_mode_writes_nothing_and_returns_both_days(tmp_path):
    ideas, q = _hunter_setup(tmp_path)
    before = q.read_text("utf-8")
    r = DD.run_deep_dive(now=D1, queue_path=q, notices_path=tmp_path / "n.jsonl",
                         passports_dir=tmp_path / "pass", max_per_run=1, ideas_dir=ideas, dry=True,
                         challenge_fn=lambda a, candidate=None: _stub_challenge(a))
    assert q.read_text("utf-8") == before
    assert not (tmp_path / "n.jsonl").exists() and not (tmp_path / "pass").exists()
    assert len(r["тексты"]) == 2 and "день первый" in r["тексты"][0] and "$47.50" in r["тексты"][1]
