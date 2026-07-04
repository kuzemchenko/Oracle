# -*- coding: utf-8 -*-
"""Тесты точечного состязательного разбора идеи по возражению владельца (orchestrator/challenge.py
+ параметр user_doubt в debate.run_debate).

Проверяет:
  • возражение владельца доходит ДО критика (линия атаки) И до судьи (отдельный вопрос);
  • run_debate с user_doubt=None ведёт себя как раньше (обратная совместимость, регрессия);
  • сборка кандидата из карточки §8 без выдумок; хронологический выбор «последней» идеи;
  • run_challenge на mock даёт протокол с вердиктом и человеческим резюме; журналирует на диск;
  • честный ОТКАЗ на пустом возражении / ненайденной идее.
"""
import sys
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import openrouter as OR      # noqa: E402
from orchestrator import context as C          # noqa: E402
from orchestrator import synthesis as SY       # noqa: E402
from orchestrator import debate as DBT         # noqa: E402
from orchestrator import challenge as CH       # noqa: E402


class CapturingClient(OR.MockClient):
    """MockClient, который запоминает user-промпты по агентам (чтобы проверить, что возражение
    реально вшито в дело критика/судьи), но даёт валидный mock-вывод."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen = {}

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        self.seen[agent_id] = user
        return super().complete(role, system, user, agent_id=agent_id,
                                output_kind=output_kind, exclude_family=exclude_family)


# ── возражение доходит до критика и судьи ─────────────────────────────────────────
def _debate_with_doubt(doubt):
    ctx = C.build_context(theme="brent")
    cli = CapturingClient(run_id="t_challenge")
    costs = SY.load_costs()
    cand = {"актив": "BNO.US", "направление": "лонг", "тезис": "t", "разрешимость": "§9 r",
            "школа": "тест"}
    deb = DBT.run_debate(cand, ctx, cli, run_id="t_challenge", costs=costs, user_doubt=doubt)
    return deb, cli


def test_doubt_reaches_critic_and_judge():
    doubt = "СПЕЦМАРКЕР_ВОЗРАЖЕНИЯ_42"
    deb, cli = _debate_with_doubt(doubt)
    assert doubt in cli.seen["e_critic"], "возражение должно попасть в дело критика (линия атаки)"
    assert doubt in cli.seen["e_judge"], "возражение должно попасть в дело судьи (отдельный вопрос)"
    # генератор НЕ должен видеть возражение (он защищает исходный тезис «вслепую»)
    assert doubt not in cli.seen["e_generator"]
    # протокол фиксирует возражение для журнала
    assert deb["возражение_владельца"] == doubt


def test_doubt_none_is_backward_compatible():
    deb, cli = _debate_with_doubt(None)
    assert deb["возражение_владельца"] is None
    # без возражения ключи-маркеры в делах не появляются
    assert "возражение_владельца" not in cli.seen["e_critic"]
    assert "возражение_владельца" not in cli.seen["e_judge"]
    # контур по-прежнему даёт корректный вердикт
    assert deb["вердикт"]["исход"] in ("УСТОЯЛА", "РАЗБИТА", "ВЕТО")


# ── сборка кандидата из карточки §8 без выдумок ───────────────────────────────────
def test_candidate_from_card_extracts_thesis_and_resolvability():
    card = {
        "актив": "BNO.US", "направление": "лонг",
        "запечатанный_прогноз_§9": {"прогноз_§9_preview": "BNO.US ≥ X к дате Y"},
        "отчёт": {"judgment": {"поля": {
            "1_актив_направление_инструмент": "BNO.US лонг (ETF)",
            "2_каскадная_цепочка": ["событие", "перенос на BNO.US"],
            "3_вероятность_и_калибровка": "P=0.55",
        }}},
    }
    cand = CH.candidate_from_card(card)
    assert cand["актив"] == "BNO.US" and cand["направление"] == "лонг"
    assert "BNO.US" in cand["тезис"] and "каскад" not in cand["тезис"].lower()
    assert cand["разрешимость"] == "BNO.US ≥ X к дате Y"


def test_chronological_pick_prefers_timestamped_over_fixture():
    # фикстура без timestamp в run_id идёт НИЖЕ боевого прогона с timestamp
    fixture = {"run_id": "week7_testday"}
    real = {"run_id": "multi_20260614T002206Z__x"}
    assert CH._ts_key(real) > CH._ts_key(fixture)


# ── полный прогон challenge на mock ───────────────────────────────────────────────
def test_run_challenge_explicit_candidate_mock(tmp_path):
    cand = {"актив": "BNO.US", "направление": "лонг", "тезис": "нефть вверх",
            "разрешимость": "§9: BNO.US +5% за месяц", "школа": "тест"}
    p = CH.run_challenge("а не отыграно ли уже всё?", candidate=cand, mode="mock",
                         write=True, out_dir=tmp_path, client=OR.MockClient(run_id="t"))
    assert p["mode"] == "mock"
    assert p["дебаты"]["вердикт"]["исход"] in ("УСТОЯЛА", "РАЗБИТА", "ВЕТО")
    assert "Вердикт слепого судьи" in p["резюме"]
    assert p["возражение_владельца"]
    # журнал записан (ничего не удаляем — CLAUDE.md)
    assert (tmp_path / f"{p['run_id']}.json").exists()


def test_run_challenge_refuses_empty_doubt():
    cand = {"актив": "BNO.US", "направление": "лонг"}
    p = CH.run_challenge("   ", candidate=cand, mode="mock", client=OR.MockClient(run_id="t"))
    assert "ОТКАЗ" in p


def test_run_challenge_refuses_unknown_idea(tmp_path):
    p = CH.run_challenge("сомнение", asset="НЕТ_ТАКОГО.US", mode="mock",
                         logs_dir=tmp_path, client=OR.MockClient(run_id="t"))
    assert "ОТКАЗ" in p


# ── дайджест разборов для /review-week (мостик «вопросы → предложения», §25/§10) ──────
def _write_challenge(tmp, run_id, mode, verdict, scores, missing, ts="2026-06-14T00:00:00Z"):
    deb = {"вердикт": {"исход": verdict,
                       "средний_балл_рубрики": round(sum(scores.values()) / len(scores), 2)},
           "реплики": {
               "судья": {"ok": True, "judgment": {"рубрика": {"оценки": [
                   {"критерий": k, "балл": v, "обоснование": "x"} for k, v in scores.items()]}}},
               "reviewer_данных": {"ok": True, "judgment": {"находки": [
                   {"объект": m, "статус": "отсутствует в данных"} for m in missing]}}}}
    p = {"run_id": run_id, "ts": ts, "mode": mode, "идея": {"актив": "CLF.US", "направление": "лонг"},
         "возражение_владельца": "а где данные по дефициту?", "дебаты": deb}
    (tmp / f"{run_id}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")


def test_digest_aggregates_live_only_and_flags_weak_and_gaps(tmp_path):
    _write_challenge(tmp_path, "challenge_A", "live", "РАЗБИТА",
                     {"causal_chain_strength": 1, "net_asymmetry": 2, "resolvability_p9": 4},
                     ["дефицит GOES-стали", "backlog"])
    _write_challenge(tmp_path, "challenge_B", "live", "РАЗБИТА",
                     {"causal_chain_strength": 2, "net_asymmetry": 4, "resolvability_p9": 4},
                     ["дефицит GOES-стали"])
    _write_challenge(tmp_path, "challenge_M", "mock", "УСТОЯЛА",
                     {"causal_chain_strength": 5}, ["мок-пробел"])
    import json as _j, sys as _s, pathlib as _p
    _s.path.insert(0, str(_p.Path(__file__).resolve().parents[2]))
    from orchestrator import challenge as CH
    dg = CH.digest_challenges(logs_dir=tmp_path)
    assert dg["n_разборов"] == 2 and dg["n_mock_пропущено"] == 1     # mock исключён (П16)
    assert dg["по_вердикту"] == {"РАЗБИТА": 2}
    # самый повторяющийся слабый критерий — causal_chain_strength (низкий в обоих)
    top = dg["слабые_критерии_рубрики"][0]
    assert top["критерий"] == "causal_chain_strength" and top["n_низких"] == 2
    # resolvability_p9 не слабый (4 ≥ 3) → не в низких
    assert all(c["критерий"] != "resolvability_p9" or c["n_низких"] == 0
               for c in dg["слабые_критерии_рубрики"])
    # повторяющийся пробел в данных — «дефицит GOES-стали» (×2), мок-пробел отсутствует
    gaps = {g["сигнал"]: g["частота"] for g in dg["пробелы_в_данных"]}
    assert gaps.get("дефицит GOES-стали") == 2
    assert "мок-пробел" not in gaps
    assert "усиление" in CH.format_digest(dg) or "слабые критерии" in CH.format_digest(dg)


def test_digest_empty_when_no_live(tmp_path):
    import sys as _s, pathlib as _p
    _s.path.insert(0, str(_p.Path(__file__).resolve().parents[2]))
    from orchestrator import challenge as CH
    dg = CH.digest_challenges(logs_dir=tmp_path)
    assert dg["n_разборов"] == 0
    assert "Предлагать нечего" in CH.format_digest(dg)


def test_ts_key_mixed_formats_sort_chronologically():
    # M9 (ревью 04.07): ISO и компакт сравнимы — компакт больше не «всегда новее».
    from orchestrator.challenge import _ts_key
    older_compact = {"run_id": "challenge_20260701T090000Z"}
    newer_iso = {"ts": "2026-07-04T09:00:00+00:00"}
    assert _ts_key(older_compact) < _ts_key(newer_iso)


def test_find_idea_exact_ticker_match(tmp_path):
    # Кросс-№5 (HIGH): «AA» не должен находить AAPL; «SO» — USO.US.
    import json as _json
    d = tmp_path / "logs"; d.mkdir()
    proto = {"run_id": "funnel_20260704T090000Z", "ts": "2026-07-04T09:00:00+00:00", "mode": "live",
             "этап6_синтез": {"отчёты": [
                 {"актив": "AAPL.US", "направление": "лонг", "тезис": "т1"},
                 {"актив": "USO.US", "направление": "лонг", "тезис": "т2"}]}}
    (d / "funnel_20260704T090000Z.json").write_text(_json.dumps(proto, ensure_ascii=False))
    from orchestrator.challenge import find_idea
    assert find_idea(asset="AA", logs_dir=d)[0] is None       # подстрока — не совпадение
    assert find_idea(asset="SO", logs_dir=d)[0] is None
    got, _ = find_idea(asset="AAPL", logs_dir=d)
    assert got is not None                                     # точный тикер (без .US) находится


def test_find_idea_two_way_us_suffix_and_dotted(tmp_path):
    # Кросс-№6: AAPL↔AAPL.US в обе стороны; BRK не находит BRK.B.
    import json as _json
    d = tmp_path / "logs"; d.mkdir()
    proto = {"run_id": "funnel_20260704T090001Z", "ts": "2026-07-04T09:00:01+00:00", "mode": "live",
             "этап6_синтез": {"отчёты": [{"актив": "AAPL", "направление": "лонг", "тезис": "т"},
                                          {"актив": "BRK.B", "направление": "лонг", "тезис": "т"}]}}
    (d / "funnel_20260704T090001Z.json").write_text(_json.dumps(proto, ensure_ascii=False))
    from orchestrator.challenge import find_idea
    assert find_idea(asset="AAPL.US", logs_dir=d)[0]["актив"] == "AAPL"   # .US ↔ без
    assert find_idea(asset="BRK", logs_dir=d)[0] is None                   # точечный тикер не срезан
    assert find_idea(asset="BRK.B", logs_dir=d)[0]["актив"] == "BRK.B"


def test_context_injection_overrides_none_placeholder(monkeypatch):
    # Кросс-№7 (HIGH): ctx.quotes[sym]=None (пустышка) не блокирует инъекцию истории из oracle.db.
    from orchestrator import challenge as CH
    fake_ctx = {"quotes": {"CLF.US": None}, "indicators": {"CLF.US": None}}
    monkeypatch.setattr(CH.C, "build_context", lambda theme=None, **k: dict(fake_ctx))
    injected = []
    monkeypatch.setattr(CH.C, "_quotes", lambda con, sym: injected.append(sym) or
                        [{"date": "2026-07-01", "close": 10.0, "adjusted_close": 10.0}])
    monkeypatch.setattr(CH.C, "_indicators", lambda q: {"rsi": 50})
    monkeypatch.setattr(CH.DBT, "run_debate",
                        lambda cand, ctx, cli, **k: {"вердикт": {"исход": "УСТОЯЛА"},
                                                     "реплики": {}, "актив": cand["актив"]})
    r = CH.run_challenge("сомнение", candidate={"актив": "CLF.US", "направление": "лонг",
                                                "тезис": "т", "разрешимость": None},
                         mode="mock", write=False)
    assert injected == ["CLF.US"]                       # инъекция сработала несмотря на None-ключ
    assert "дебаты" in r
