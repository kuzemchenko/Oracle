# -*- coding: utf-8 -*-
"""Ежедневная самостоятельная работа комитета (ops/daily_committee.py): темы, бюджет, экстренное сообщение, строка утром."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import bot as BOT                 # noqa: E402
import bot_reports as R           # noqa: E402
import daily_committee as DC      # noqa: E402
import dig_offer as DG            # noqa: E402
import idea_verdict as IV         # noqa: E402
import search_queue as SQ         # noqa: E402

NOW = datetime.datetime(2026, 11, 23, 6, 0, tzinfo=datetime.timezone.utc)
CFG = {"порог_сообщения": 7, "бюджет_идеи_usd": 4.0, "ходов_на_идею": 2, "идей_в_день": 4}


def _dig(tmp_path, topics=("Атака на судно в Балтике", "Налог ЕС на технокомпании"), ts=None):
    d = tmp_path / "dig"
    d.mkdir(exist_ok=True)
    pr = {"run_id": "dig_20261123T050000Z", "ts": (ts or NOW - datetime.timedelta(hours=1)).isoformat(),
          "предложения": [{"тема": t, "что_случилось": "событие", "почему_дальше": "продолжится",
                           "круги": ["1-й круг: перевозчики"], "почему_рынок_не_видит": "считает разовым",
                           "что_опровергнет": "нет повторов"} for t in topics]}
    (d / "dig_20261123T050000Z.json").write_text(json.dumps(pr, ensure_ascii=False), encoding="utf-8")
    return d


def _full_data():
    return {
        "факты": [{"текст": "факт 1", "url": "https://a.example/1", "опубликовано": "2026-11-20"},
                  {"текст": "факт 2", "url": "https://b.example/2", "опубликовано": "2026-11-21"}],
        "механизм": "страховка военного риска дорожает и поднимает ставки фрахта перевозчика",
        "chain": [{"text": "атака → страховка дороже", "quality": "confirmed", "url": "https://a.example/1"},
                  {"text": "ставки фрахта → выручка компании", "quality": "confirmed", "url": "https://b.example/2"}],
        "materiality": {"effect_profit_per_year": 300e6, "years": 3, "net_income": 1000e6, "market_cap": 4000e6},
        "durability": {"reason": "контракты на перевозку подписаны на год вперёд", "url": "https://c.example/3"},
        "market_view": {"kind": "magnitude", "metric": "выручка 2027, $ млрд", "market": {"value": 10.0, "source": "консенсус"},
                        "ours": {"value": 11.5}, "why_different": "аналитики ещё не внесли рост ставок в свои модели прибыли",
                        "основано_на": [1, 2]},
        "precedent": {"what": "атаки 2019 года в Оманском заливе", "amplitude": "+25%", "url": "https://d.example/4"},
        "сценарии": {"цена": 100.0, "дата_цены": "2026-11-20", "источник_цены": "EODHD",
                     "базовый": {"цена": 130.0, "основание": "прибыль на акцию 6.5 × мультипликатор 20"},
                     "неблагоприятный": {"цена": 88.0, "основание": "атак больше нет — возврат к уровню до новости"}},
        "направление": "long", "горизонт": "2027-02-15",
    }


def _answer(score_high=True, cost=1.5):
    def answer(q, history, tools):
        s = SQ.active("telegram", tools.search_dir)
        if not s["гипотезы"]:
            h, _ = SQ.add_hypothesis(s, "Перевозчик выигрывает от роста ставок фрахта после атаки", "TNK",
                                     "рост страховки военного риска поднимает ставки фрахта", beneficiary="Танкер — перевозит нефть")
            if score_high:
                h["данные"] = _full_data()
                SQ.record_critique(h, "google", "gemini", [{"текст": "атаки могут прекратиться через неделю", "существенное": True}])
                h["данные"]["критика"]["возражения"][0]["ответ"] = "учтено в неблагоприятном сценарии: цена 88"
                SQ.record_judgement(h, "openai", "astra", {c: {"по_делу": True, "почему": "по делу"} for c in IV.JUDGED.values()},
                                    {"1": {"снято": True, "почему": "учтено в сценарии"}})
            SQ.save(s, tools.search_dir)
        return "коротко: записал", [], cost
    return answer


def test_topics_skip_what_owner_took_or_declined_and_stale_menu(tmp_path):
    d = _dig(tmp_path, ("Тема А про танкеры", "Тема Б про налог", "Тема В про уран"))
    dec = tmp_path / "dec.jsonl"
    dec.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in (
        {"файл": "dig_20261123T050000Z", "решение": "копаем", "тема": "Тема А про танкеры"},
        {"файл": "dig_20261123T050000Z", "решение": "мимо", "тема": "Тема Б про налог"},
        {"файл": "dig_20261123T050000Z", "решение": "не выбрано", "тема": "Тема В про уран"})), encoding="utf-8")
    got = DC.topics(NOW, d, dec)
    assert [t["тема"] for t in got] == ["Тема В про уран"] and "сначала глубина" in got[0]["запрос"]
    assert DC.topics(NOW, d, dec, done={"Тема В про уран"}) == []
    assert DC.topics(NOW + datetime.timedelta(hours=40), d, dec) == []          # вчерашнее меню не копаем


def test_high_score_with_judge_makes_urgent_message_only_behind_flag(tmp_path):
    d = _dig(tmp_path, ("Атака на судно в Балтике",))
    kw = dict(now=NOW, budget=15.0, out_dir=tmp_path / "out", search_root=tmp_path / "s", dig_dir=d,
              decisions=tmp_path / "none.jsonl", answer=_answer(), notices=tmp_path / "n.jsonl", cfg=CFG, log=lambda *a: None)
    rec = DC.run(push=False, **kw)
    idea = rec["идеи"][0]
    assert idea["балл"] >= 7 and idea["судья"] is True and idea["тикер"] == "TNK" and idea["ходов"] == 2
    assert idea["отправлено"] is False and not (tmp_path / "n.jsonl").exists()   # флаг выключен — владельцу ничего
    txt = idea["экстренное_сообщение"]
    assert txt.startswith("Самостоятельная работа: идея набрала") and "Танкер (TNK)" in txt and DC.FRAME in txt
    assert "не вероятность роста" in txt and len(txt) <= DC.MAX_LEN
    assert rec["потрачено_usd"] == 3.0 and json.loads((tmp_path / "out" / "2026-11-23.json").read_text(encoding="utf-8"))["идеи"]
    assert DC.run(push=True, **kw)["идеи"] == rec["идеи"]                         # вторая попытка в тот же день тему не повторяет
    saved = IV.saved()
    assert len(saved) == 1 and saved[0]["тикер"] == "TNK"                          # вердикт записан до исхода


def test_urgent_message_is_sent_when_flag_is_on_and_not_without_judge(tmp_path):
    d = _dig(tmp_path, ("Атака на судно в Балтике",))
    kw = dict(now=NOW, budget=15.0, search_root=tmp_path / "s", dig_dir=d, decisions=tmp_path / "none.jsonl",
              notices=tmp_path / "n.jsonl", cfg=CFG, log=lambda *a: None)
    rec = DC.run(push=True, out_dir=tmp_path / "o1", answer=_answer(), **kw)
    sent = [json.loads(x) for x in (tmp_path / "n.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rec["идеи"][0]["отправлено"] is True and len(sent) == 1 and not sent[0].get("silent")
    low = DC.run(push=True, out_dir=tmp_path / "o2", answer=_answer(score_high=False), **kw)["идеи"][0]
    assert "экстренное_сообщение" not in low and len((tmp_path / "n.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_day_budget_caps_the_work(tmp_path):
    d = _dig(tmp_path, ("Тема А про танкеры", "Тема Б про налог", "Тема В про уран"))
    rec = DC.run(now=NOW, budget=5.0, push=False, out_dir=tmp_path / "out", search_root=tmp_path / "s", dig_dir=d,
                 decisions=tmp_path / "none.jsonl", answer=_answer(score_high=False, cost=2.0), cfg=CFG, log=lambda *a: None)
    assert [i["тема"] for i in rec["идеи"]] == ["Тема А про танкеры"]      # остаток $1 минус запас — на новую идею не хватает
    assert rec["потрачено_usd"] == 4.0                                       # день не вышел за потолок $5
    assert DC.run(now=NOW, budget=0.0, out_dir=tmp_path / "o3", dig_dir=d, answer=_answer(), cfg=CFG,
                  log=lambda *a: None)["идеи"] == []                                          # бюджета нет — не работаем


def test_morning_line_reports_yesterday_work(tmp_path):
    assert DC.morning_line(NOW, tmp_path) is None
    DC.save_day({"дата": "2026-11-22", "потрачено_usd": 4.2, "идеи": [
        {"тема": "Атака на судно в Балтике", "тикер": "TNK", "имя": "Танкер", "балл": 5, "из": 10, "решение_словами": "архив",
         "не_хватает": ["судья: довод не по делу — нет расчёта прироста"]},
        {"тема": "Налог ЕС", "причины_отказа": ["META: эффект уже в цене"]}]}, tmp_path)
    line = DC.morning_line(NOW, tmp_path)
    assert line.startswith("Вчера копал сам (потрачено $4.20)") and "Танкер (TNK): 5 из 10, архив; не хватило: судья" in line
    assert "«Налог ЕС» — отклонена (META: эффект уже в цене)" in line and "не вероятность роста" in line
    DC.save_day({"дата": "2026-11-23", "потрачено_usd": 4.1, "идеи": [{"тема": "Налог ЕС", "отклонено": 7, "причины_отказа": [
        "META: механизм опровергнут: Три независимых разрушителя механизма: (1) ФАКТ НЕ СУЩЕСТВУЕТ — первичного документа нет"]}]}, tmp_path)
    assert "«Налог ЕС» — отклонена (гипотез 7; первая — META: механизм опровергнут)." in DC.morning_line(NOW, tmp_path)


def test_owner_topic_runs_inside_the_same_day_budget(tmp_path):
    d = _dig(tmp_path, ("Тема из утреннего меню",))
    t = DC.owner_topic("Распродажа ИИ: кто упал зря", "S&P 500 за неделю минус 8.6%")
    assert "direction 'short'" in t["запрос"] and "минус 8.6%" in t["запрос"] and "не покупать" in t["запрос"]
    rec = DC.run(now=NOW, budget=4.0, push=False, out_dir=tmp_path / "out", search_root=tmp_path / "s", dig_dir=d,
                 decisions=tmp_path / "none.jsonl", answer=_answer(score_high=False, cost=1.0), cfg=CFG, log=lambda *a: None,
                 extra_topics=[t, DC.owner_topic("Распродажа ИИ: кто упадёт дальше")])
    assert [i["тема"] for i in rec["идеи"]] == ["Распродажа ИИ: кто упал зря", "Распродажа ИИ: кто упадёт дальше"]
    assert rec["потрачено_usd"] == 4.0 and rec["идеи"][1]["ходов"] == 2          # вторая тема — на остаток дня за вычетом запаса


def test_morning_message_gets_the_line_only_behind_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(DC, "morning_line", lambda now=None, **k: "Вчера копал сам: проверка.")
    pr = {"run_id": "dig_x", "ts": NOW.isoformat(), "текст": "Что покопать сегодня."}
    monkeypatch.setattr(R, "load_presentation", lambda *a, **k: {"daily_committee_push": False})
    DG._write(dict(pr), tmp_path)
    assert json.loads((tmp_path / "dig_x.json").read_text(encoding="utf-8"))["текст"] == "Что покопать сегодня."
    monkeypatch.setattr(R, "load_presentation", lambda *a, **k: {"daily_committee_push": True})
    DG._write(dict(pr), tmp_path)
    assert json.loads((tmp_path / "dig_x.json").read_text(encoding="utf-8"))["текст"].endswith("Вчера копал сам: проверка.")


def test_bot_knows_the_job_and_flags_are_off_until_owner_ok():
    job = [j for j in BOT.SCHEDULED_JOBS if j[0] == "daily_committee_auto"]
    assert job and job[0][2][:2] == ("ops/daily_committee.py", "run") and job[0][4] == (5, 30)
    assert {"daily_committee_auto", "daily_committee_push"} <= set(R.JOB_FLAGS)
    assert DC.day_budget({"per_run_token_budget_usd": {}}) == 0.0
