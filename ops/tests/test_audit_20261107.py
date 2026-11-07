# -*- coding: utf-8 -*-
"""Аудит 07.11 (DHT, MP, FSLR): адресные регрессии. Каждый тест воспроизводит конкретную ошибку
аудита на ИСХОДНЫХ формулировках и проверяет, что система теперь ловит её ДО отправки владельцу."""
import copy
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "ops")):
    if p not in sys.path:
        sys.path.insert(0, p)

import bot_agent as A                    # noqa: E402
import claim_check as CC                 # noqa: E402
import evidence_check as EV              # noqa: E402
import idea_gate as G                    # noqa: E402
import idea_revision as IR               # noqa: E402
import agent_facts as F                  # noqa: E402
import hunter_seal as HS                 # noqa: E402
import bot as BOT                        # noqa: E402
import bot_state as S                    # noqa: E402

IDEAS = ROOT / "reports" / "ideas"
CARDS = {"MP": "idea_2026-11-02_MP", "DHT": "idea_2026-11-03_DHT", "FSLR": "idea_2026-11-04_FSLR"}


def card(t):
    return json.loads((IDEAS / (CARDS[t] + ".card.json")).read_text(encoding="utf-8"))


# ── №2: регуляторика, исключения, «не нашёл» ─────────────────────────────────────────
def test_fslr_exception_in_facts_ignored_by_thesis_is_caught():
    r = EV.check(card("FSLR"))
    assert any("исключение из правила" in f and "факт 2" in f for f in r["fails"])
    assert any("регуляторный катализатор без проверки полного текста" in f for f in r["fails"])


def test_fslr_fixed_regulatory_block_passes_regulatory_rules():
    c = card("FSLR")
    c["regulatory"] = {
        "document_url": "https://www.federalregister.gov/x", "quote": "minimum import price of $0.38 per watt for modules",
        "effective_date": "2026-12-04", "scope": "кремниевые модули, ячейки, пластины, поликремний",
        "transition": "ввоз впрок ограничен квотами до 04.12 (цитата …)",
        "amendments_checked": {"checked_at": "2026-11-07", "sources": ["https://www.federalregister.gov/x"]},
        "exceptions": [{"text": "контракты с фиксированным сроком, подписанные до 06.08.2026",
                        "effect": "эффект растянут: часть поставок 2027 идёт по старым ценам",
                        "kill_review": "стоп по сводкам цен считает только контракты после 06.08"}]}
    r = EV.check(c)
    assert not any("исключени" in f and "не учитывают" in f for f in r["fails"])
    # тезис «нельзя продать дешевле» без оговорки исключения — отдельный брак
    assert any("абсолютно" in f for f in r["fails"])
    c["thesis"] = c["thesis"].replace("нельзя продать", "нельзя продать (кроме исключения для контрактов до 06.08)")
    assert not any("абсолютно" in f for f in EV.check(c)["fails"])


def test_mp_absence_claim_needs_check_record_and_secondary_only_is_labelled():
    c = card("MP")
    r = EV.check(c)
    assert any("ОТСУТСТВИИ" in f for f in r["fails"])
    c["absence_checks"] = [{"claim": "поправки к №70 нет", "sources_checked": ["https://carraglobe.com/x"],
                            "primary_checked": False, "checked_at": "2026-11-03"}]
    r = EV.check(c)
    assert not any("ОТСУТСТВИИ" in f for f in r["fails"])
    assert any("вторичным источникам" in x for x in r["labels"])


# ── №1: время события ────────────────────────────────────────────────────────────────
def test_dht_earnings_catalyst_needs_time_and_session():
    c = card("DHT")
    assert any("без времени публикации" in f for f in EV.check(c)["fails"])
    c["catalyst"]["datetime_utc"], c["catalyst"]["session"] = "2026-11-09T21:05:00Z", "regular"
    assert any("сессия катализатора" in f for f in EV.check(c)["fails"])
    c["catalyst"]["session"] = "after_close"
    assert not any("сессия" in f or "без времени" in f for f in EV.check(c)["fails"])


def test_agent_reply_mp_report_reaction_flagged_without_event_reaction():
    reply = "Отчёт Q3 вышел 05.11 после закрытия. — Рынок на отличный отчёт ответил падением −6% (05.11)."
    codes = [f["code"] for f in CC.check(reply, trace=[])]
    assert "timing" in codes
    assert "timing" not in [f["code"] for f in CC.check(reply, trace=[{"tool": "event_reaction"}])]


# ── №3: показатели ───────────────────────────────────────────────────────────────────
def test_mp_eps_without_basis_and_revenue_one_off_flagged():
    s = "Отчёт Q3 (05.11, после закрытия): прибыль $0.04 против консенсуса −$0.01, выручка $147.7 млн (+176% г/г)."
    codes = {f["code"] for f in CC.check(s, trace=[{"tool": "event_reaction"}])}
    assert {"eps_basis", "revenue_oneoff"} <= codes
    ok = ("Скорректированная прибыль (adjusted) $0.04 против консенсуса −$0.01 на том же базисе, по GAAP — убыток $0.12; "
          "выручка $147.7 млн, в том числе разовая продажа SEG+ $31.6 млн.")
    assert not {"eps_basis", "revenue_oneoff"} & {f["code"] for f in CC.check(ok, trace=[])}


def test_card_metric_basis_and_consensus_comparability():
    c = card("FSLR")
    assert any("финансовый показатель без определения" in f for f in EV.check(c)["fails"])
    i = next(i for i, f in enumerate(c["facts"]) if "на акцию" in f["text"])
    c["facts"][i]["metric"] = {"name": "EPS", "value": 5.30, "unit": "$", "period": "Q3 2026", "basis": "GAAP",
                               "one_offs_checked": True}
    assert not any(f"факт {i + 1}:" in f for f in EV.check(c)["fails"])
    c["facts"][i]["text"] += " Консенсус $4.10."
    c["facts"][i]["metric"]["consensus"] = {"value": 4.10, "basis": "adjusted"}
    assert any("несопоставимо" in f for f in EV.check(c)["fails"])


# ── №4–5: цель и расчёт ─────────────────────────────────────────────────────────────
def test_targets_without_calc_are_provisional_and_labelled_in_push():
    for t in CARDS:
        r = EV.check(card(t))
        assert r["target"]["status"] == "предварительная"
        res = HS.preview(IDEAS / CARDS[t])
        assert "Цель предварительная" in res["text"]


def test_dht_target_calc_must_reproduce():
    c = card("DHT")
    c["target_basis"] = {"method": "yield", "formula": "div_annual / y",
                         "inputs": {"div_annual": 6.4, "y": 0.2},
                         "input_sources": {"div_annual": "4 × квартальный дивиденд по брони Q4 (допущение)",
                                           "y": "дивидендная доходность DHT в 2024–2025"},
                         "assumptions": ["бронь Q4 повторяется 4 квартала"]}
    assert any("не воспроизводится" in f or "даёт" in f for f in EV.check(c)["fails"])   # 32 ≠ 27
    c["target_basis"]["inputs"]["div_annual"] = 5.4
    r = EV.check(c)
    assert not any(f.startswith("цель:") for f in r["fails"]) and r["target"]["status"] == "обоснована"


def test_dht_tce_called_revenue_flagged():
    s = "Выручка рейсов: 78% × 2055 судо-дней × $225 200 в сутки ≈ $361 млн, минус издержки $61 млн."
    assert "tce" in {f["code"] for f in CC.check(s)}
    ok = s + " Это выручка TCE, уже за вычетом рейсовых расходов."
    assert "tce" not in {f["code"] for f in CC.check(ok)}


# ── №6: пересмотр не переписывает историю ───────────────────────────────────────────
def test_revision_proposals_flagged_in_chat():
    for s in ("Стоп придётся пересматривать от новой цены.", "Предлагаю перенести проверку на I квартал 2027 года."):
        assert "revision" in {f["code"] for f in CC.check(s)}
    ok = "Это была бы новая версия идеи; исходный прогноз не меняется и будет сверен как записан."
    assert "revision" not in {f["code"] for f in CC.check("Предлагаю перенести проверку на март. " + ok)}


def test_revision_file_keeps_original_and_forbids_bet_change_as_fact_fix(tmp_path):
    stem = tmp_path / "idea_2026-11-04_FSLR"
    src = card("FSLR")
    (tmp_path / "idea_2026-11-04_FSLR.card.json").write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"idea_2026-11-04_FSLR": {"sha256": HS.card_sha256(src), "pinned_at": "x"}}))
    with pytest.raises(ValueError):
        IR.new_revision(stem, "fact_correction", "исключение", {"spec9.resolve_by": "2027-03-31"},
                        out_dir=tmp_path / "rev", pred_path=tmp_path / "none.jsonl", pins_path=pins)
    out = IR.new_revision(stem, "new_hypothesis", "эффект растянут исключением", {"spec9.resolve_by": "2027-03-31"},
                          date="2026-11-07", out_dir=tmp_path / "rev", pred_path=tmp_path / "none.jsonl", pins_path=pins)
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["changes"]["spec9.resolve_by"] == {"old": "2027-01-08", "new": "2027-03-31"}
    assert json.loads((tmp_path / "idea_2026-11-04_FSLR.card.json").read_text(encoding="utf-8")) == src
    # исходник после этого правят «в лоб» — verify ловит
    src2 = copy.deepcopy(src)
    src2["stop_price"] = 150
    (tmp_path / "idea_2026-11-04_FSLR.card.json").write_text(json.dumps(src2, ensure_ascii=False), encoding="utf-8")
    preds = tmp_path / "p.jsonl"
    preds.write_text(json.dumps({"kind": "hunter_idea", "idea_file": "idea_2026-11-04_FSLR.push.txt"}) + "\n")
    bad = IR.verify(ideas_dir=tmp_path, pred_path=preds, pins_path=pins)
    assert bad and "изменена после записи" in bad[0]


def test_real_original_cards_unchanged():
    assert IR.verify() == []


def test_seal_record_carries_card_hash_and_probability_kind():
    c = card("DHT")
    spec = HS.spec_from_card(c, idea_file="idea_2026-11-03_DHT.push.txt")
    assert spec["карточка_sha256"] == HS.card_sha256(c) and spec["probability_kind"] == "subjective"
    assert spec["цель_статус"] == "предварительная"


# ── №7: вероятности и статистика ─────────────────────────────────────────────────────
def test_dht_wrong_sealed_probability_and_coin_claim_flagged():
    s = ("Шанс на «выше 25.50 к 29.01» я бы сейчас дал около 30% против ~55% на момент запечатывания. "
         "И напомню: исторически наши прогнозы пока на уровне монетки.")
    codes = {f["code"] for f in CC.check("DHT. " + s, trace=[], sealed={"DHT": 0.3})}
    assert {"sealed_prob", "track"} <= codes
    ok = "DHT: при записи в журнал стояло 30% — это субъективная оценка охотника, не калиброванная."
    assert "sealed_prob" not in {f["code"] for f in CC.check(ok, trace=[], sealed={"DHT": 0.3})}


def test_track_record_separates_mechanisms(tmp_path):
    p, o = tmp_path / "p.jsonl", tmp_path / "o.jsonl"
    rows = [{"hash": f"h{i}", "kind": "cascade_provisional", "probability": 0.5, "sealed_at": "2026-07-01"} for i in range(40)]
    rows.append({"hash": "hx", "kind": "hunter_idea", "probability": 0.3, "sealed_at": "2026-11-04"})
    p.write_text("\n".join(json.dumps(r) for r in rows))
    o.write_text("\n".join(json.dumps({"hash": f"h{i}", "outcome": i % 2}) for i in range(40)))
    t = {r["механизм"]: r for r in F.track_record(p, o)["механизмы"]}
    assert t["hunter_idea"]["сверено"] == 0 and "сказать нечего" in t["hunter_idea"]["вывод"]
    assert t["provisional"]["сверено"] == 40 and "brier_база_константа" in t["provisional"]


# ── №8: статус цепочки ───────────────────────────────────────────────────────────────
def test_pipeline_status_marks_old_funnel_and_disabled_deep_dive(tmp_path):
    (tmp_path / "ideas" / "claims").mkdir(parents=True)
    (tmp_path / "ideas" / "claims" / "2026-11-07.txt").write_text("10:00 по запросу владельца, медь\n")
    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "ef_20261107T090001Z.json").write_text("{}")
    dd = tmp_path / "dd.jsonl"
    dd.write_text(json.dumps({"ts": "2026-11-04T15:58", "asset": "DHT.US", "status": "queued"}) + "\n")
    st = F.pipeline_status(pred_path=tmp_path / "none", deliv_path=tmp_path / "none", deep_path=dd,
                           ideas_dir=tmp_path / "ideas", funnel_dir=tmp_path / "f", flags={"deep_dive_auto": False})
    assert "не завершена" in st["охота_по_запросу"][0]["этап"]
    assert "ВЫКЛЮЧЕНА" in st["прокачка_«Качаем»"][0]["статус"]
    assert "НЕ охотник" in st["старый_конвейер"]["что_это"] and "ВЫКЛЮЧЕН" in st["старый_конвейер"]["доставка"]
    assert "pipeline" in {f["code"] for f in CC.check("Прогон ef_20261107T090001Z с 16 идеями подтверждает работу системы.")}


def test_kachaem_message_says_deep_dive_is_off():
    assert "ВЫКЛЮЧЕНА" in BOT.deep_dive_status_line({"deep_dive_auto": False})
    assert "включена" in BOT.deep_dive_status_line({"deep_dive_auto": True})


# ── №9: обрыв ответа ─────────────────────────────────────────────────────────────────
def _msg(content=None, finish="stop", calls=None):
    m = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                           for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": m, "finish_reason": finish}], "usage": {"cost": 0.01}}


class FakeLLM:
    def __init__(self, script):
        self.script, self.bodies = list(script), []

    def __call__(self, body):
        self.bodies.append(body)
        return self.script.pop(0)


def _agent(script):
    return A.Agent(post=FakeLLM(script), tools=A.Tools(), api_key="x", log_cost=lambda *a, **k: None,
                   sleep=lambda s: None)


def test_length_cut_answer_is_continued_not_returned_as_done():
    ag = _agent([_msg("Аудит выполнен. Первая часть разбора про DHT и про стоимость фрах", "length"),
                 _msg("тования судов. Вторая часть: выводы.", "stop")])
    text, _f, _c = ag.run("аудит")
    assert "фрахтования судов" in text and ag.continued == 1 and not ag.truncated


def test_still_truncated_answer_is_marked():
    ag = _agent([_msg("часть", "length")] + [_msg(" ещё", "length")] * A.MAX_CONTINUATIONS)
    text, _f, _c = ag.run("аудит")
    assert ag.truncated and "оборван" in text


def test_claim_findings_go_back_for_repair_then_footer():
    bad = "Рынок на отличный отчёт ответил падением −6% (05.11)."
    ag = _agent([_msg(bad), _msg(bad)])          # модель не исправилась
    text, _f, _c = ag.run("что с MP")
    assert ag.findings_first and ag.findings_left
    assert "Проверка кодом" in text
    ag2 = _agent([_msg(bad),
                  _msg(None, calls=[("event_reaction", {"ticker": "MP", "event_time_utc": "2026-11-05T21:05:00Z"})]),
                  _msg("Падение 05.11 было до отчёта; реакцию показала сессия 06.11 — причина хода пока гипотеза.")])
    text2, _f, _c = ag2.run("что с MP")
    assert not ag2.findings_left and "Проверка кодом" not in text2
    assert "[система] Проверка кодом" in json.dumps(ag2.post.bodies[1]["messages"], ensure_ascii=False)


def test_split_message_never_cuts_words_and_send_long_numbers_and_retries(tmp_path, monkeypatch):
    text = ("Абзац про DHT. " * 200 + "\n\n" + "Второй абзац про MP и FSLR. " * 200).strip()
    parts = BOT.split_message(text, 3888)
    assert "".join(p.replace(" ", "") for p in parts) == text.replace(" ", "").replace("\n", "")
    assert all(len(p) <= 3888 for p in parts) and all(not p[-1].isalpha() or p.endswith(("DHT", "MP", "FSLR")) for p in parts)

    class TG:
        def __init__(self):
            self.sent, self.fail_once = [], True

        def send_message(self, chat, t, reply_markup=None):
            if self.fail_once and t.startswith("(2/"):
                self.fail_once = False
                return None
            self.sent.append(t)
            return len(self.sent)
    monkeypatch.setattr(BOT, "DELIVERIES_PATH", tmp_path / "d.jsonl")
    b = BOT.Bot(TG(), "1", state=S._default_state())
    res = b._send_long("1", text, kind="ответ агента", sleep=lambda s: None)
    assert res["parts"] == len(parts) >= 2 and res["sent"] == res["parts"] and not res["failed"]
    assert b.tg.sent[0].startswith(f"(1/{len(parts)}) ")
    rec = json.loads((tmp_path / "d.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["parts_sent"] == rec["parts"]


def test_gate_enforces_evidence_for_new_hunts_only():
    import datetime
    c = card("FSLR")
    old = G.check(c, today=datetime.date(2026, 11, 4), horizon_from=datetime.date(2026, 11, 4))
    new = G.check(c, today=datetime.date(2026, 11, 8), horizon_from=datetime.date(2026, 11, 8))
    assert not any(f.startswith("доказательства:") for f in old["fails"]) and old["warnings"]
    assert any(f.startswith("доказательства:") for f in new["fails"])
