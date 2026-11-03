# -*- coding: utf-8 -*-
"""Тесты V5 «Шансы недели» (orchestrator/week_odds.py + подача + бот-флаг).

Закрепляют:
  • генерация: кандидаты экономиста → детерминированная §9-валидация (горизонт строго 3–13
    недель, тикер *.US, порог/вероятность в рамках) — брак отбрасывается с причиной (П8);
  • печать в герметичный трек thesis_event: разрешимость §9, дедуп той же ставки при повторе;
  • подача format_week_odds проходит блокирующий линт (тикеры с именами, секции, без тех.id);
  • бот: пуш ТОЛЬКО за флагом week_odds_push (правило внедрения 01.11), дедуп по протоколу.
Герметично: LLM — стаб, журналы и протоколы — tmp, сети нет.
"""
import json
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from orchestrator import week_odds as WO     # noqa: E402
import bot_state as S                        # noqa: E402
import bot as BOT                            # noqa: E402
import bot_reports as R                      # noqa: E402

NOW = datetime.datetime(2026, 11, 6, 8, 10, tzinfo=datetime.timezone.utc)


class StubClient:
    """Детерминированный «экономист»: 4 кандидата — 2 валидных, 2 брака."""
    def __init__(self):
        self.calls = 0

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        self.calls += 1
        cands = [
            {"thesis_id": "T1_потоки_критических_активов", "mechanism_id": "energy_supply_shock",
             "вопрос": "Нефть (фонд USO) закроется выше $80 к середине декабря",
             "актив": "USO.US", "направление": "above", "порог": 80.0,
             "срок_недель": 6, "вероятность": 0.35,
             "почему": "Запасы низкие, а предложение зажато — дефицит толкает цену вверх."},
            {"thesis_id": None, "mechanism_id": "chokepoint_disruption",
             "вопрос": "Фрахт танкеров: Frontline выше 30 к началу января",
             "актив": "FRO.US", "направление": "above", "порог": 30.0,
             "срок_недель": 9, "вероятность": 0.55,
             "почему": "Обходы узких мест удлиняют маршруты — провозная ёмкость сжимается."},
            {"mechanism_id": "energy_supply_shock", "вопрос": "Нефть за два дня вырастет",
             "актив": "USO.US", "направление": "above", "порог": 81.0,
             "срок_недель": 1, "вероятность": 0.6, "почему": "слишком коротко"},
            {"mechanism_id": None, "вопрос": "Brent вырастет",
             "актив": "BRENT", "направление": "above", "порог": 90.0,
             "срок_недель": 6, "вероятность": 0.5, "почему": "не тикер"},
        ]
        return {"text": json.dumps(cands, ensure_ascii=False), "model": "stub", "usage": {}, "cost": 0.0}


def _theses(tmp_path):
    p = tmp_path / "theses.yaml"
    p.write_text("тезисы:\n  T1_потоки_критических_активов:\n    формулировка: тестовый тезис\n",
                 encoding="utf-8")
    return p


def _mechs(tmp_path):
    p = tmp_path / "mechs.yaml"
    p.write_text(json.dumps({"механизмы": [
        {"id": "energy_supply_shock", "rule": "r", "trigger_class": "t", "tradeable": True,
         "lag_weeks": "2-8", "watch_assets": "нефть", "amplitude": "+30–130% за 2–10 недель",
         "precedents": [{"year": "2022", "observed": "+35%"}]},
        {"id": "chokepoint_disruption", "rule": "r", "trigger_class": "t", "tradeable": True,
         "lag_weeks": "1-6", "watch_assets": "танкеры", "amplitude": "+20–60% за 2–6 недель",
         "precedents": [{"year": "2024", "observed": "+40%"}]},
    ]}, ensure_ascii=False), encoding="utf-8")
    return p


def test_priced_in_annotation_sealed_and_rendered(tmp_path):
    """«Что уже в цене» (подпись 01.11): ход против прецедентной амплитуды попадает
    в запечатанную запись и в подачу; нет цены/механизма — честно без поля."""
    preds = tmp_path / "predictions.jsonl"
    r = WO.run_week_odds(mode="mock", seal=True, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), predictions_path=preds,
                         out_dir=tmp_path / "wo", client=StubClient(),
                         move_fn=lambda a: 40.0 if a == "USO.US" else None)
    qs = {q["asset"]: q for q in r["вопросы"]}
    pi = qs["USO.US"]["что_уже_в_цене"]
    assert pi["вердикт"] == "БОЛЬШАЯ ЧАСТЬ В ЦЕНЕ" and "прецедентного хода" in pi["строка"]
    assert "что_уже_в_цене" not in qs["FRO.US"], "нет цены → честно без поля"
    rows = [json.loads(x) for x in preds.read_text("utf-8").splitlines()]
    sealed = {x["asset"]: x for x in rows}
    assert sealed["USO.US"]["что_уже_в_цене"]["вердикт"] == "БОЛЬШАЯ ЧАСТЬ В ЦЕНЕ"
    text, viol = R.format_week_odds(r)
    assert not viol and "Что уже в цене:" in text


def test_forward_score_on_views_and_phrases(tmp_path):
    """Счёт форварда НА взгляде (П16: только из журнала): агрегация по hash-join,
    вопрос с тезисом И механизмом кормит оба счёта; фразы честные."""
    import forward_score as FS
    preds = tmp_path / "p.jsonl"
    outs = tmp_path / "o.jsonl"
    rows = [
        {"kind": "thesis_event", "hash": "h1", "mechanism_id": "energy_supply_shock",
         "thesis_id": "T1_потоки_критических_активов"},
        {"kind": "thesis_event", "hash": "h2", "mechanism_id": "energy_supply_shock"},
        {"kind": "thesis_event", "hash": "h3", "mechanism_id": "chokepoint_disruption"},
        {"kind": "cascade_provisional", "hash": "h4", "mechanism_id": "energy_supply_shock"},
    ]
    preds.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), "utf-8")
    outs.write_text("\n".join([json.dumps({"hash": "h1", "outcome": 1}),
                               json.dumps({"hash": "h2", "outcome": 0})]), "utf-8")
    s = FS.view_scores(predictions_path=preds, outcomes_path=outs)
    assert s["mechanism:energy_supply_shock"] == {"вопросов": 2, "исходов": 2, "попаданий": 1}
    assert s["thesis:T1_потоки_критических_активов"] == {"вопросов": 1, "исходов": 1, "попаданий": 1}
    assert s["mechanism:chokepoint_disruption"]["исходов"] == 0
    assert "чужой трек не подмешан" and "mechanism:energy_supply_shock" in s
    ph = FS.score_phrase(s["mechanism:energy_supply_shock"])
    assert "50%" in ph and "1 из 2" in ph and "выборка мала" in ph
    assert "исходов ещё нет" in FS.score_phrase(s["mechanism:chokepoint_disruption"])
    assert "копится" in FS.score_phrase(None)


def test_week_odds_render_human_view_names(tmp_path):
    """Подача: владелец видит имя взгляда и счёт, НЕ тех.id механизма."""
    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), out_dir=tmp_path / "wo",
                         client=StubClient())
    scores = {"mechanism:energy_supply_shock": {"вопросов": 3, "исходов": 2, "попаданий": 1}}
    text, viol = R.format_week_odds(r, scores=scores)
    assert not viol
    assert "взгляд «шок предложения энергии»" in text
    assert "сбывался в 50%" in text
    assert "(взгляд: energy_supply_shock" not in text


def _quotes_db(tmp_path, rows):
    import sqlite3
    db = tmp_path / "oracle.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, open REAL, high REAL, "
                "low REAL, close REAL, adjusted_close REAL, volume INTEGER)")
    con.executemany("INSERT INTO quotes (symbol, date, close, adjusted_close) "
                    "VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def test_make_move_fn_window_and_staleness(tmp_path):
    """Серверный move_fn: ход за окно недель по quotes; протухшие/дырявые данные → None."""
    db = _quotes_db(tmp_path, [
        ("USO.US", "2026-09-25", 70.0, 70.0),     # начало окна 6 нед от 06.11
        ("USO.US", "2026-10-15", 80.0, 80.0),
        ("USO.US", "2026-11-05", 98.0, 98.0),     # свежая
        ("OLD.US", "2026-09-25", 50.0, 50.0),
        ("OLD.US", "2026-10-01", 55.0, 55.0),     # последняя старше 2 недель → протухло
        ("GAP.US", "2026-10-30", 10.0, 10.0),
        ("GAP.US", "2026-11-05", 11.0, 11.0),     # нет данных на начало окна → дыра
    ])
    mv = WO.make_move_fn(db_path=db, now=NOW)
    assert abs(mv("USO.US", 6) - 40.0) < 0.01     # 70 → 98 = +40%
    assert mv("OLD.US", 6) is None
    assert mv("GAP.US", 6) is None
    assert mv("NOPE.US", 6) is None
    assert WO.make_move_fn(db_path=tmp_path / "нет.db", now=NOW)("USO.US", 6) is None


def test_priced_in_two_arg_move_fn_uses_horizon(tmp_path):
    """annotate передаёт move_fn(asset, horizon_weeks) — окно хода = горизонт вопроса."""
    seen = {}

    def mv(asset, weeks=None):
        seen[asset] = weeks
        return 40.0 if asset == "USO.US" else None

    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), out_dir=tmp_path / "wo",
                         client=StubClient(), move_fn=mv)
    assert seen["USO.US"] == 6                    # срок вопроса из стаба — 6 недель
    assert any("что_уже_в_цене" in q for q in r["вопросы"])


def test_priced_in_price_source_failure_is_soft(tmp_path):
    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), out_dir=tmp_path / "wo",
                         client=StubClient(),
                         move_fn=lambda a: (_ for _ in ()).throw(RuntimeError("нет данных")))
    assert r["принято"] == 2 and all("что_уже_в_цене" not in q for q in r["вопросы"])


def test_run_validates_and_seals(tmp_path):
    preds = tmp_path / "predictions.jsonl"
    r = WO.run_week_odds(mode="mock", seal=True, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), predictions_path=preds,
                         out_dir=tmp_path / "wo", client=StubClient())
    assert r["кандидатов"] == 4 and r["принято"] == 2 and r["запечатано"] == 2
    причины = " | ".join(b["причина"] for b in r["брак"])
    assert "вне зоны 3–13" in причины and "не EODHD-тикер" in причины
    rows = [json.loads(x) for x in preds.read_text("utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["track"] == "thesis_event" and rows[0]["kind"] == "thesis_event"
    assert rows[0]["resolve_by"].startswith("2026-12-18")        # 6 недель от 06.11
    assert rows[0]["hash"] and rows[0]["mechanism_id"] == "energy_supply_shock"
    # протокол записан
    files = list((tmp_path / "wo").glob("wo_*.json"))
    assert len(files) == 1


def test_rerun_dedups_same_bets(tmp_path):
    preds = tmp_path / "predictions.jsonl"
    kw = dict(mode="mock", seal=True, now=NOW, theses_path=_theses(tmp_path),
              mechs_path=_mechs(tmp_path), predictions_path=preds, out_dir=tmp_path / "wo")
    WO.run_week_odds(client=StubClient(), **kw)
    r2 = WO.run_week_odds(client=StubClient(), **kw)
    assert r2["запечатано"] == 0
    # аудит 03.11: повтор ловит уже квота «бумага × неделя сверки» (раньше — только дубль ставки)
    assert sum(("дубль в журнале" in b["причина"]) or ("уже открыт вопрос" in b["причина"])
               for b in r2["брак"]) == 2
    rows = preds.read_text("utf-8").splitlines()
    assert len(rows) == 2, "повторный прогон не должен плодить дубли ставок"


def test_economist_refusal_is_honest(tmp_path):
    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("нет сети")
    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), out_dir=tmp_path / "wo", client=Boom())
    assert "ОТКАЗ" in r and r["принято"] == 0


def test_format_week_odds_passes_lint(tmp_path):
    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), out_dir=tmp_path / "wo", client=StubClient())
    text, viol = R.format_week_odds(r)
    assert viol == [] and text
    assert text.startswith("Шансы недели — ")
    assert "около 35%" in text and "около 55%" in text
    assert "не инвестиционная рекомендация" in text
    # базовый тикер уже назван в вопросе («фонд USO») → скобки-дубля [… (USO.US)] нет
    assert "USO" in text and "[нефть WTI (USO.US)]" not in text
    assert "Frontline (FRO.US)" in text                          # не названный — дополняется именем


def test_bot_pushes_only_behind_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), out_dir=tmp_path / "journal" / "week_odds",
                         client=StubClient())
    assert r["принято"] == 2

    class T:
        def __init__(self):
            self.sent = []
        def send_message(self, chat, text, reply_markup=None):
            self.sent.append(text)
            return 1
        def get_updates(self, o, timeout=0):
            return []
    # флаг выключен (дефолт) → тишина
    b = BOT.Bot(T(), "5", state=S._default_state())
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"week_odds_push": False})
    b._tick_week_odds()
    assert b.tg.sent == []
    # флаг включён → один пуш, повтор — дедуп. Ре-гейт 02.11: тик пропускает
    # mock/без печатей — дорисуем протоколу «боевой» вид прямо в файле
    import json as _json
    f = sorted((tmp_path / "journal" / "week_odds").glob("wo_*.json"))[-1]
    proto = _json.loads(f.read_text("utf-8"))
    proto["mode"], proto["запечатано"] = "live", len(proto.get("вопросы") or [])
    proto["печать_запрошена"] = True              # аудит 03.11: сухой показ (False) бот пропускает молча
    f.write_text(_json.dumps(proto, ensure_ascii=False), "utf-8")
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"week_odds_push": True})
    b._tick_week_odds()
    assert len(b.tg.sent) == 1 and "Шансы недели" in b.tg.sent[0]
    b._tick_week_odds()
    assert len(b.tg.sent) == 1


def test_sealable_and_anchor_guards(tmp_path):
    """Ре-гейт V0 02.11: (§9) тикер без источника цены — брак; (§10) порог без якоря к цене
    или вне полосы 0.5×..2× — брак; с котировкой и разумным порогом — печатается."""
    kw = dict(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
              mechs_path=_mechs(tmp_path), out_dir=tmp_path / "wo", client=StubClient())
    # sealable_fn: USO есть, FRO нет → FRO бракуется с причиной §9
    r = WO.run_week_odds(sealable_fn=lambda a: a == "USO.US", **kw)
    assert r["принято"] == 1 and r["вопросы"][0]["asset"] == "USO.US"
    assert any("нет источника цены" in b["причина"] for b in r["брак"])
    # last_close_fn: порог 80 при цене 70 — в полосе И на дальней стороне; при 500 —
    # вырожденный; None — нет якоря
    r2 = WO.run_week_odds(last_close_fn=lambda a: 70.0 if a == "USO.US" else 25.0, **kw)
    assert r2["принято"] == 2                     # оба порога в полосе и на дальней стороне
    r2b = WO.run_week_odds(last_close_fn=lambda a: 70.0, **kw)
    assert r2b["принято"] == 1                    # FRO (порог 30) при цене 70 — вне полосы
    # Перепроверка №2 02.11: above с порогом НИЖЕ цены — почти гарантированный YES,
    # трек мерил бы тривиальность порога, а не взгляд → брак
    r2c = WO.run_week_odds(last_close_fn=lambda a: 100.0 if a == "USO.US" else 25.0, **kw)
    assert any("уже-истинной" in b["причина"] for b in r2c["брак"])
    assert all(q["asset"] != "USO.US" for q in r2c["вопросы"])
    r3 = WO.run_week_odds(last_close_fn=lambda a: 500.0, **kw)
    assert r3["принято"] == 0 and all("не заякорен" in b["причина"] for b in r3["брак"][:2])
    r4 = WO.run_week_odds(last_close_fn=lambda a: None, **kw)
    assert r4["принято"] == 0 and any("нет котировки для якоря" in b["причина"] for b in r4["брак"])


def test_tick_skips_unsealed_and_notices_lint_fail(tmp_path, monkeypatch):
    """Ре-гейт V0 02.11 (П8): mock/несекретный протокол владельцу не уходит; провал линта —
    заметка в дозор, не молчание."""
    import json as _json
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    sys.path.insert(0, str(ROOT / "ops" / "tests"))
    from test_bot_v2_loop import _bot
    b = _bot()
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
    monkeypatch.setattr(b, "_notice_owner", lambda t: notes.append(t))      # аудит 03.11: владельцу
    monkeypatch.setattr(R, "load_presentation", lambda path=None: {"week_odds_push": True})
    d = tmp_path / "journal" / "week_odds"
    d.mkdir(parents=True)
    (d / "wo_1.json").write_text(_json.dumps({"mode": "mock", "запечатано": 0, "печать_запрошена": True, "вопросы": [
        {"вопрос": "x", "asset": "USO.US", "probability": 0.4}]}, ensure_ascii=False), "utf-8")
    b._tick_week_odds()
    assert not b.tg.sent and "wo_1.json" in b.state["week_odds_sent"]
    # ре-гейт №2 02.11: пропуск при включённом боевом флаге — всегда аномалия, дозор не молчит
    assert notes and "без печатей" in notes[0]
    # запечатанный, но битый по подаче (нет вопросов → format вернёт None с причиной)
    (d / "wo_2.json").write_text(_json.dumps({"mode": "live", "запечатано": 2, "печать_запрошена": True,
                                              "вопросы": []}, ensure_ascii=False), "utf-8")
    b._tick_week_odds()
    assert not b.tg.sent and any("не прошли проверку подачи" in n for n in notes)


def test_format_unknown_ticker_does_not_kill_issue(tmp_path):
    """Незнакомый тикер → пометка «компанию не опознал», выпуск жив и линт-чист."""
    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                         mechs_path=_mechs(tmp_path), out_dir=tmp_path / "wo",
                         client=StubClient())
    import unittest.mock as um
    with um.patch.object(R, "_asset_human", lambda t: (t, "")):
        text, viol = R.format_week_odds(r)
    assert viol == [] and text.count("компанию не опознал") >= 2


def test_cli_show_prints_owner_text(tmp_path, monkeypatch, capsys):
    """03.11: ручной прогон `week_odds.py --show` печатает подачу ровно как в Telegram (без печати)."""
    monkeypatch.setattr(WO, "make_client", lambda **k: StubClient())
    monkeypatch.setattr(WO, "THESES", _theses(tmp_path))
    monkeypatch.setattr(WO, "MECHS", _mechs(tmp_path))
    monkeypatch.setattr(WO, "OUT_DIR", tmp_path / "wo")
    monkeypatch.setattr(WO, "make_move_fn", lambda: (lambda *a, **k: None))
    assert WO.main(["--mode", "mock", "--show"]) == 0
    out = capsys.readouterr().out
    assert "[week_odds]" in out and ("не инвестиционная рекомендация" in out or "ПОДАЧА НЕ ПРОШЛА" in out)


def test_threshold_from_move_and_server_price_not_llm_memory():
    """03.11 (ручной прогон для «ок»): экономист ставил пороги по памяти (USO $85 при $156.87) →
    6/8 в брак. Теперь он задаёт ход в %, порог считает код от серверной цены, текст вопроса — код."""
    c = {"thesis_id": "T1", "mechanism_id": "energy_supply_shock",
         "вопрос": "Нефть (фонд USO) закроется выше $85 к 15 января?", "актив": "USO.US",
         "направление": "above", "ход_проц": 8.0, "срок_недель": 6, "вероятность": 0.35,
         "почему": "дефицит предложения"}
    spec, why = WO.validate_candidate(c, NOW, sealable_fn=lambda a: True, last_close_fn=lambda a: 156.87)
    assert why is None and spec["threshold"] == round(156.87 * 1.08, 2)
    assert "$169.42" in spec["вопрос"] and "$85" not in spec["вопрос"] and spec["вопрос"].startswith("Нефть (фонд USO)")
    bad, why2 = WO.validate_candidate({**c, "ход_проц": -5.0}, NOW, sealable_fn=lambda a: True,
                                      last_close_fn=lambda a: 156.87)
    assert bad is None and "знак хода" in why2
    tiny, why3 = WO.validate_candidate({**c, "ход_проц": 0.4}, NOW, sealable_fn=lambda a: True,
                                       last_close_fn=lambda a: 156.87)
    assert tiny is None and "шуме" in why3


def test_prompt_carries_server_prices_and_named_company_not_flagged(tmp_path):
    mechs = [{"mechanism_id": "m", "watch": "энергосектор (XLE/XOP), фонды USO/UNG; TTF как датчик"}]
    tk = WO.watch_tickers(mechs)
    assert tk[:4] == ["XLE", "XOP", "USO", "UNG"] and "TTF" not in tk
    u = WO.build_user_prompt({}, mechs, NOW, prices={"USO.US": 156.87})
    assert "СВЕЖИЕ ЦЕНЫ ЗАКРЫТИЯ" in u and "USO.US: 156.87" in u
    proto = {"ts": NOW.isoformat(), "вопросы": [{"asset": "ZZQQ.US", "direction": "above", "threshold": 330.0,
             "вопрос": "Constellation Energy (ZZQQ) закроется выше $330.00 к 12 января",
             "probability": 0.56, "почему": "спрос на мощность"}]}
    text, viol = R.format_week_odds(proto)
    assert text and "не опознал" not in text, viol



# ── аудит 03.11: перестройка V5 до первой боевой печати ─────────────────────────────────────────
def test_rotation_quotas_ids_and_noise_base(tmp_path):
    """Ротация окна механизмов по номеру прогона; выдуманный/«null» id — брак; ≤2 на механизм и
    одна бумага на прогон; в печать — база шума, якорь с датой, ход в σ; ход меньше шума — брак."""
    mp = tmp_path / "m.yaml"
    mp.write_text(json.dumps({"механизмы": [
        {"id": f"m{i:02d}", "rule": "r", "tradeable": True, "precedents": [{"year": "2020", "observed": "+20%"}]}
        for i in range(20)]}, ensure_ascii=False), encoding="utf-8")
    w0 = [m["mechanism_id"] for m in WO.load_mechanisms(mp, top=14, rotation=0)]
    w1 = [m["mechanism_id"] for m in WO.load_mechanisms(mp, top=14, rotation=1)]
    assert w0[0] == "m00" and w1[0] == "m14" and set(w0) | set(w1) == {f"m{i:02d}" for i in range(20)}

    def cand(asset, mid, move, weeks=4, tid=None):
        return {"thesis_id": tid, "mechanism_id": mid, "вопрос": f"{asset} закроется выше",
                "актив": asset, "направление": "above" if move > 0 else "below", "ход_проц": move,
                "срок_недель": weeks, "вероятность": 0.4, "почему": "потому что механизм"}

    class C:
        mode = "mock"

        def complete(self, *a, **k):
            return {"text": json.dumps([
                cand("AAA.US", "m01", 12), cand("BBB.US", "m01", 12), cand("CCC.US", "m01", 12),  # 3-й — квота
                cand("DDD.US", "m99", 12), cand("EEE.US", "null", 12),                           # выдуман / без взгляда
                cand("FFF.US", "m02", 1.5),                                                      # меньше шума
                cand("AAA.US", "m03", -12)], ensure_ascii=False)}                                 # бумага повторно
    closes = [100 * (1.01 if i % 2 else 0.99) for i in range(60)] + [100.0]
    r = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path), mechs_path=mp,
                         out_dir=tmp_path / "wo", client=C(), last_close_fn=lambda a: 100.0,
                         closes_fn=lambda a: (closes, "2026-11-05"))
    assert [q["asset"] for q in r["вопросы"]] == ["AAA.US", "BBB.US"]
    why = " | ".join(b["причина"] for b in r["брак"])
    for need in ("квота: уже 2 вопроса", "не из библиотеки", "без взгляда", "о шуме", "по этой бумаге уже"):
        assert need in why, need
    q = r["вопросы"][0]
    assert q["якорь_цена"] == 100.0 and q["якорь_дата"] == "2026-11-05" and q["ход_проц"] == 12.0
    assert 0 < q["вероятность_база"] < 0.5 and q["ход_в_сигмах"] > WO.MIN_SIGMA_MOVE
    assert r["печать_запрошена"] is False and r["квота_сроков"]["выполнена"] is True



def test_bot_skips_dry_show_silently_and_pushes_latest_sealed(tmp_path, monkeypatch):
    """Аудит 03.11: ручной --show (печать не запрошена) — не тревога крона; из нескольких
    неотправленных запечатанных уходит свежий."""
    import json as _json
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    d = tmp_path / "journal" / "week_odds"
    d.mkdir(parents=True)
    base = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                            mechs_path=_mechs(tmp_path), out_dir=tmp_path / "x", client=StubClient())
    for name, extra in (("wo_20261103T081000Z.json", {"mode": "live", "запечатано": 2, "печать_запрошена": True}),
                        ("wo_20261105T081000Z.json", {"mode": "live", "запечатано": 2, "печать_запрошена": True}),
                        ("wo_20261105T120000Z.json", {"mode": "live", "запечатано": 0, "печать_запрошена": False})):
        (d / name).write_text(_json.dumps({**base, **extra, "run_id": name[:-5]}, ensure_ascii=False), "utf-8")

    class T:
        def __init__(self):
            self.sent = []

        def send_message(self, chat, text, reply_markup=None):
            self.sent.append(text)
            return 1
    b = BOT.Bot(T(), "5", state=S._default_state())
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
    monkeypatch.setattr(b, "_notice_owner", lambda t: notes.append(t))      # аудит 03.11: владельцу
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"week_odds_push": True})
    b._tick_week_odds()
    assert len(b.tg.sent) == 1 and not notes
    assert set(b.state["week_odds_sent"]) == {"wo_20261103T081000Z.json", "wo_20261105T081000Z.json",
                                              "wo_20261105T120000Z.json"}


def test_track_score_line_from_journal_not_constant():
    assert "исходов пока нет" in R.track_score_line(None)
    t = {"исходов": 12, "сбылось": 5, "база_ожидание": 4.1, "умение": {"verdict": "данных мало"}}
    line = R.track_score_line(t)
    assert "сверено 12, сбылось 5" in line and "около 4.1" in line and "данных мало" in line
    assert "вердикт: лучше шума" in R.track_score_line({**t, "умение": {"verdict": "лучше шума"}})



def test_watermark_no_carousel_after_50_protocols(tmp_path, monkeypatch):
    """Ре-гейт 03.11: sent обрезается до 50 — без водяного знака после 51-го протокола старые выпуски
    уходили бы снова на каждом тике. Протокол без поля «печать_запрошена» (до 03.11) — сухой, молча."""
    import json as _json
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    d = tmp_path / "journal" / "week_odds"
    d.mkdir(parents=True)
    base = WO.run_week_odds(mode="mock", seal=False, now=NOW, theses_path=_theses(tmp_path),
                            mechs_path=_mechs(tmp_path), out_dir=tmp_path / "x", client=StubClient())

    class T:
        def __init__(self):
            self.sent = []

        def send_message(self, chat, text, reply_markup=None):
            self.sent.append(text)
            return 1
    b = BOT.Bot(T(), "5", state=S._default_state())
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
    monkeypatch.setattr(b, "_notice_owner", lambda t: notes.append(t))
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"week_odds_push": True})
    (d / "wo_20261103T000000Z.json").write_text(_json.dumps({**base, "mode": "live"}, ensure_ascii=False), "utf-8")
    for i in range(60):
        (d / f"wo_202611{10 + i // 24:02d}T{i % 24:02d}0000Z.json").write_text(_json.dumps(
            {**base, "mode": "live", "запечатано": 2, "печать_запрошена": True}, ensure_ascii=False), "utf-8")
        b._tick_week_odds()
    n = len(b.tg.sent)
    assert n == 60 and not notes                                   # по одному пушу на выпуск, без тревог
    for _ in range(3):
        b._tick_week_odds()
    assert len(b.tg.sent) == n                                     # карусели нет


def test_more_than_8_questions_all_named():
    qs = [{"asset": f"USO.US", "вопрос": f"Нефть закроется выше ${80 + i} к 1 декабря", "probability": 0.4,
           "почему": "запасы низкие"} for i in range(10)]
    text, viol = R.format_week_odds({"ts": "2026-11-10T08:10:00+00:00", "вопросы": qs})
    assert not viol and "Ещё 2 вопрос(а) записаны в счёт" in text
