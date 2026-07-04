# -*- coding: utf-8 -*-
"""Тесты режима §17.6 «Сессия партнёра» (П3, REVISION_2026-07 §R3): сборка из протокола,
границы П2б (порядок = выдача прогона), журнал сессий, метрики §R5, рендер, интенты бота."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from orchestrator import partner_session as PS   # noqa: E402

ASOF = "2026-07-04T18:00:00+00:00"


def _proto():
    return {
        "run_id": "ef_test", "ts": "2026-07-04T09:00:00+00:00",
        "граф_отбор": {
            "money_трек": [
                {"актив": "GEV.US", "направление": "лонг", "edge": 0.06, "цепочка": "ai_power",
                 "порядок": 2, "чокпоинт": True, "надёжность_r2": 0.4, "лаг_дней": 5,
                 "вероятность": 0.62, "провизорный": False,
                 "внимание": {"статус": "ok", "ключ": "transformer shortage", "фаза": "РАНО",
                              "свежесть": 0.9}},
            ],
            "провизорный_трек": [
                {"актив": "CLF.US", "направление": "лонг", "edge": 0.09, "цепочка": "ai_power",
                 "порядок": 3, "провизорный": True,
                 "внимание": {"статус": "не_измерено", "причина": "нет данных"}},
                {"актив": "GEV.US", "направление": "лонг", "провизорный": True},   # дубль по активу
            ],
            "суд_money": {"GEV.US": {"исход": "УСТОЯЛА", "балл": 3.4,
                                     "кто_против": "продают пассивные ребалансировки",
                                     "почему_возможность": "дефицит трансформаторов не в цене"}},
        },
        "картограф_идеи": [
            {"актив": "CCJ.US", "событие": "Казахстан ограничил экспорт урана",
             "узлы_каскада": [{"порядок": 2, "узел": "дефицит топлива АЭС", "чокпоинт": True}],
             "тектонический_потенциал": 0.7,
             "внимание": {"статус": "не_измерено", "причина": "ключ назначен, ждём фетча"}},
        ],
    }


def test_build_session_order_dedup_and_fields():
    s = PS.build_session(_proto(), asof=ASOF)
    активы = [i["актив"] for i in s["идеи"]]
    # граница П2б: порядок = существующая выдача (money → провизорный → картограф), дубль схлопнут
    assert активы == ["GEV.US", "CLF.US", "CCJ.US"]
    gev = s["идеи"][0]
    assert gev["суд"]["исход"] == "УСТОЯЛА"
    assert "продают" in gev["суд"]["кто_продаёт_нам"]
    assert gev["внимание"]["фаза"] == "РАНО"
    clf = s["идеи"][1]
    assert clf["суд"]["исход"] is None and "не гонялся" in clf["суд"]["пометка"]   # честно
    ccj = s["идеи"][2]
    assert "уран" in ccj["аргумент"]["событие"]
    assert s["мало_идей"] is False and s["идей"] == 3
    assert s["схлопнуто_дублей_актива"] == 1                  # дубль GEV.US схлопнут ЯВНЫМ правилом
    assert "также_в" in s["идеи"][0]                          # и помечен на карточке


def test_build_session_honest_refusal_and_stale():
    assert "ОТКАЗ" in PS.build_session(None, asof=ASOF)
    old = _proto(); old["ts"] = "2026-06-20T09:00:00+00:00"
    s = PS.build_session(old, asof=ASOF)
    assert "ОТКАЗ" in s and "устарели" in s["ОТКАЗ"]          # кросс-ревью BLOCKER: не греем вчерашнее
    assert "идеи" not in s
    broken = PS.build_session({"_битый_протокол": "ef_x.json"}, asof=ASOF)
    assert "ОТКАЗ" in broken and "нечитаем" in broken["ОТКАЗ"]  # без подмены старым


def test_build_session_survives_malformed_protocol():
    # Кросс-ревью HIGH: кривые, но валидные JSON-формы не роняют сессию.
    for bad in ({"ts": ASOF, "граф_отбор": []},
                {"ts": ASOF, "граф_отбор": {"money_трек": [42]}},
                {"ts": ASOF, "граф_отбор": {"суд_money": []},
                 "картограф_идеи": [{"актив": "X.US", "узлы_каскада": [1]}]}):
        s = PS.build_session(bad, asof=ASOF)
        assert "идей" in s                                    # собралось (пусть и пусто), не упало


def test_record_session_refuses_refusal(tmp_path):
    p = tmp_path / "s.jsonl"
    assert PS.record_session({"ОТКАЗ": "нет протокола"}, path=p) is None
    assert not p.exists()                                     # журнал не загрязнён


def test_session_cap_and_few_ideas_flag():
    p = _proto()
    p["граф_отбор"]["провизорный_трек"] = [
        {"актив": f"A{i}.US", "провизорный": True} for i in range(10)]
    s = PS.build_session(p, asof=ASOF)
    assert s["идей"] == PS.SESSION_MAX                        # кэп 5
    p2 = {"run_id": "x", "ts": ASOF, "граф_отбор": {}, "картограф_идеи": []}
    s2 = PS.build_session(p2, asof=ASOF)
    assert s2["идей"] == 0 and s2["мало_идей"] is True        # П8: не добираем выдумками


def test_record_and_metrics(tmp_path):
    sessions = tmp_path / "sessions.jsonl"
    ch_dir = tmp_path / "challenges"; ch_dir.mkdir()
    s = PS.build_session(_proto(), asof=ASOF)
    PS.record_session(s, path=sessions)
    rec = json.loads(sessions.read_text().splitlines()[0])
    assert rec["активы"] == ["GEV.US", "CLF.US", "CCJ.US"]
    # два challenge в окне: один УСТОЯЛА, один РАЗБИТА; один — вне окна
    for name, ts, исход in (("challenge_a", "2026-07-03T10:00:00+00:00", "УСТОЯЛА"),
                            ("challenge_b", "2026-07-04T10:00:00+00:00", "РАЗБИТА"),
                            ("challenge_old", "2026-06-01T10:00:00+00:00", "УСТОЯЛА")):
        (ch_dir / f"{name}.json").write_text(json.dumps(
            {"ts": ts, "дебаты": {"вердикт": {"исход": исход}}}, ensure_ascii=False))
    (ch_dir / "challenge_nc.json").write_text(json.dumps(
        {"ts": "2026-07-04T11:00:00+00:00",
         "дебаты": {"вердикт": {"исход": "УСТОЯЛА_БЕЗ_КРИТИКА"}}}, ensure_ascii=False))
    m = PS.session_metrics(asof=ASOF, sessions_path=sessions, challenges_dir=ch_dir)
    assert m["сессий"] == 1 and m["retention_ok"] is True and m["сессий_за_7дн"] == 1
    assert m["докапываний"] == 3 and m["вердиктов"] == 2
    assert m["выживаемость"] == 0.5                           # БЕЗ_КРИТИКА не в числителе И не в знаменателе
    assert m["без_критика"] == 1


def test_metrics_empty_is_honest(tmp_path):
    m = PS.session_metrics(asof=ASOF, sessions_path=tmp_path / "none.jsonl",
                           challenges_dir=tmp_path / "noch")
    assert m["сессий"] == 0 and m["retention_ok"] is False
    assert m["выживаемость"] is None                          # нет вердиктов — не 0 и не 1


def test_retention_is_weekly_regardless_of_window(tmp_path):
    # Кросс-ревью LOW: retention_ok — по последним 7 дням, даже если окно метрик шире.
    sessions = tmp_path / "s.jsonl"
    sessions.write_text(json.dumps({"ts": "2026-06-10T10:00:00+00:00", "идей": 3}) + "\n")
    m = PS.session_metrics(asof=ASOF, days=30, sessions_path=sessions,
                           challenges_dir=tmp_path / "none")
    assert m["сессий"] == 1                                   # в 30-дневном окне есть
    assert m["сессий_за_7дн"] == 0 and m["retention_ok"] is False


def test_render_partner_session():
    import bot_reports as R
    s = PS.build_session(_proto(), asof=ASOF)
    m = {"окно_дней": 7, "сессий": 2, "докапываний": 3, "выживаемость": 0.67, "вердиктов": 3}
    txt = R.format_partner_session(s, m)
    assert "Сессия партнёра" in txt
    assert "GEV.US" in txt and "CCJ.US" in txt
    assert "Кто продаёт нам" in txt
    assert "не инвестиционная рекомендация" in txt            # рамка §16 обязательна
    assert "выживаемость" in txt
    # отказ рендерится честно
    assert "не собралась" in R.format_partner_session({"ОТКАЗ": "нет протокола"})


def test_bot_session_intent():
    import bot as B
    yes = ["сессия", "дай идеи", "Идеи дня", "ок, дай идеи", "покажи идеи"]
    no = ["найди идеи", "сделай прогон", "что думаешь про уран?", "разбери GEV",
          "дай идеи по нефти на завтра с обоснованием"]
    for t in yes:
        assert B.Bot._intent_session(t), t
    for t in no:
        assert not B.Bot._intent_session(t), t
