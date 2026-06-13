# -*- coding: utf-8 -*-
"""Сквозной дымовой тест воронки (гейт Недели 5–6, MASTER_SPEC §24).

Гейт: «Сквозной прогон — все школы выдвигают кандидатов, поле суждений собирается в
стандартном формате Дирижёра». Тест прогоняет ВСЮ воронку в mock-режиме (без сети и
трат) и проверяет:
  • промпты всех агентов B/C/D/G существуют и содержат П8 (инвариант 1 CLAUDE.md);
  • оркестратор маршрутизирует каждого агента по роли models.yaml (включая фолбеки);
  • поле суждений собрано в стандартном формате §5.2 (вывод+вероятность+уверенность+
    данные-основания+что неизвестно) для каждого агента;
  • школы выдвигают кандидатов; собирается карта противоречий и контрфактический протокол §11.1;
  • программная П8-ворота работает (ловит вероятность без оснований).
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents.registry import AGENTS, schools, all_ids, all_prompt_ids  # noqa: E402
from agents import build_prompts                            # noqa: E402
from orchestrator import judgment as J                      # noqa: E402
from orchestrator import openrouter as OR                   # noqa: E402
from orchestrator import context as C                       # noqa: E402
from orchestrator import agents as A                        # noqa: E402
from orchestrator import funnel as F                        # noqa: E402

PROMPTS = ROOT / "agents" / "prompts"


# ── Промпты: все на месте, в каждом П8 (инвариант 1) ─────────────────────────────
def test_all_prompts_exist_with_p8():
    # все 28 промптов (поле B/C/D/G + состязательный контур E + синтез F)
    for aid in all_prompt_ids():
        path = PROMPTS / f"{aid}.md"
        assert path.exists(), f"нет промпта {aid}"
        text = path.read_text(encoding="utf-8")
        assert "НУЛЕВЫЕ ВЫДУМКИ" in text, f"П8 отсутствует в {aid}"
        assert "ФОРМАТ ВЫВОДА" in text, f"контракт вывода отсутствует в {aid}"


def test_prompts_in_sync_with_builder():
    # на диске ровно то, что генерит build_prompts (нет ручного дрейфа)
    for aid in all_prompt_ids():
        on_disk = (PROMPTS / f"{aid}.md").read_text(encoding="utf-8")
        assert on_disk == build_prompts.build_one(aid), f"рассинхрон промпта {aid}"


def test_blocks_complete():
    blocks = {b for _, b, *_ in AGENTS}
    assert blocks == {"B", "C", "D", "G"}
    assert len(schools()) >= 10  # 10 школ блока B + прогнозные C


# ── Маршрутизация моделей: каждая роль резолвится в models.yaml ──────────────────
def test_every_agent_role_resolves():
    models = OR.load_models()
    for aid, _b, _t, role, *_ in AGENTS:
        cfg = OR.resolve_role(role, models)
        assert cfg["primary"], f"нет primary для роли {role} ({aid})"
        assert "family" in cfg


def test_judge_family_constraint_helper():
    # П10: фолбек судьи не из семейства генератора текущей идеи
    assert OR.judge_family_ok("google/gemini-3.1-pro-preview", "anthropic")
    assert not OR.judge_family_ok("anthropic/claude-sonnet-4.6", "anthropic")


def test_fallback_chain_switches_and_logs(monkeypatch, tmp_path):
    """§26: при отказе primary оркестратор переходит на фолбек и пишет смену в протокол."""
    # перенаправляем логи в tmp, чтобы не трогать журнал
    monkeypatch.setattr(OR, "FUNNEL_LOGS", tmp_path)
    monkeypatch.setattr(OR, "COSTS_LOG", tmp_path / "costs.jsonl")

    client = OR.LiveClient.__new__(OR.LiveClient)  # без сети/ключа
    client.models = OR.load_models()
    client.run_id = "t_fb"
    client.mode = "live"
    client.cost_guard = None  # __new__ минует __init__: ставим вручную (§24)

    calls = {"n": 0}
    role_cfg = OR.resolve_role("judge", client.models)
    chain = [role_cfg["primary"]] + role_cfg["fallbacks"]

    def fake_one_call(model, cfg, system, user):
        calls["n"] += 1
        if model == chain[0]:
            raise OR._Retryable("unavailable", "primary 503")
        return ('{"вывод":"ok","вероятность":null,"уверенность":"низкая",'
                '"данные_основания":[],"что_неизвестно":[]}'), {"prompt_tokens": 1}, 0.0

    monkeypatch.setattr(client, "_one_call", fake_one_call)
    resp = client.complete("judge", "sys", "usr", agent_id="x", output_kind="control")
    assert resp["fallback_index"] == 1            # ушли на первый фолбек
    assert resp["model"] == chain[1]
    swaps = (tmp_path / "t_fb_model_swaps.jsonl")
    assert swaps.exists() and "attempted" in swaps.read_text(encoding="utf-8")


# ── Парсинг и П8-ворота ──────────────────────────────────────────────────────────
def test_p8_gate_rejects_probability_without_grounds():
    bad = '{"вывод":"лонг","вероятность":0.7,"уверенность":"высокая",' \
          '"данные_основания":[],"что_неизвестно":[],"кандидаты":[]}'
    obj = J.parse(bad, "school_judgment")
    viol = J.validate_p8(obj)
    assert viol and any("без оснований" in v for v in viol)


def test_p8_gate_accepts_no_data():
    nd = '{"вывод":"нет данных","вероятность":null,"уверенность":"низкая",' \
         '"данные_основания":[],"что_неизвестно":["мало данных"],"кандидаты":[]}'
    obj = J.parse(nd, "school_judgment")
    assert obj["_no_data"] is True
    assert J.is_clean(obj)  # «нет данных» — легитимно, не нарушение


def test_extract_json_tolerates_fences():
    obj = J.extract_json('бла бла ```json\n{"a": 1}\n``` хвост')
    assert obj == {"a": 1}


def test_control_probability_must_be_null():
    bad = '{"вывод":"ok","вердикт":"OK","вероятность":0.6,"уверенность":"высокая",' \
          '"данные_основания":[],"что_неизвестно":[],"находки":[]}'
    with pytest.raises(J.JudgmentError):
        J.parse(bad, "control")


# ── Сквозной прогон воронки в mock-режиме ────────────────────────────────────────
@pytest.fixture(scope="module")
def protocol():
    return F.run_funnel(theme="brent", mode="mock", run_id="pytest_smoke", write=False)


def test_funnel_runs_all_agents(protocol):
    assert protocol["mode"] == "mock"
    assert protocol["agents_total"] == len(AGENTS)
    assert protocol["agents_ok"] == len(AGENTS)  # все агенты дали валидный формат


def test_judgment_field_standard_format(protocol):
    field = protocol["поле_суждений"]
    assert len(field) == len(AGENTS)
    required = ("вывод", "вероятность", "уверенность", "данные_основания", "что_неизвестно")
    for row in field:
        assert row["ok"]
        for key in required:
            assert key in row, f"{row['agent']}: нет поля §5.2 '{key}'"


def test_all_schools_present_and_some_propose(protocol):
    school_ids = {s[0] for s in schools()}
    ran = {r["agent"] for r in protocol["поле_суждений"] if r["is_school"]}
    assert ran == school_ids, "не все школы попали в поле суждений"
    assert protocol["candidates_count"] > 0, "ни одна школа не выдвинула кандидата"
    assert protocol["schools_with_candidates"], "пустой список школ-кандидатов"


def test_counterfactual_protocol_present(protocol):
    cf = protocol["контрфактический_протокол"]
    assert cf["n_голосов"] >= 1
    assert cf["агрегированная_вероятность"] is not None
    # drop-one: по контрфакту на каждый чистый голос с вероятностью
    assert len(cf["контрфакты"]) == cf["n_голосов"]


def test_contradiction_map_built(protocol):
    # карта противоречий — список (возможно пустой), структура корректна
    for c in protocol["карта_противоречий"]:
        assert "лонг" in c and "шорт" in c and c["актив"]


def test_no_p8_violations_in_mock(protocol):
    # mock синтезирует только валидные суждения → процедурное вето пусто
    assert protocol["процедурное_вето"] == []


def test_data_gaps_reported_honestly(protocol):
    gaps = " ".join(protocol["data_gaps"])
    assert "OI" in gaps or "открытый интерес" in gaps  # П8: пробелы не скрыты


# ── Гард темы (§6/§8/П8): тематический фокус только по активу универсума ──────────
def test_resolve_theme_maps_name_core_and_unknown():
    assert C.resolve_theme("brent") == ("BNO.US", "theme")     # имя темы → proxy_etf
    assert C.resolve_theme("SPY.US") == ("SPY.US", "core")     # прямой тикер ядра
    assert C.resolve_theme("SPCX.US") == (None, None)          # вне универсума


def test_theme_guard_refuses_out_of_universe():
    # Тематический фокус по активу вне универсума → ранний отказ (0 трат), даже в mock.
    p = F.run_funnel(theme="SPCX.US", mode="mock", run_id="pytest_guard",
                     write=False, theme_focused=True)
    assert "ОТКАЗ_тема" in p
    assert p["ОТКАЗ_тема"]["resolvable"] is False
    assert "agents_total" not in p          # ни один агент не вызывался


def test_theme_guard_passes_calibrated_theme():
    # brent в универсуме с историей → гард пропускает, прогон идёт нормально.
    p = F.run_funnel(theme="brent", mode="mock", run_id="pytest_guard_ok",
                     write=False, theme_focused=True)
    assert "ОТКАЗ_тема" not in p
    assert p["agents_total"] >= 1


def test_theme_guard_inactive_without_focus():
    # Без theme_focused (обычная воронка) гард не трогает даже чужой тикер.
    p = F.run_funnel(theme="SPCX.US", mode="mock", run_id="pytest_guard_off",
                     write=False, theme_focused=False)
    assert "ОТКАЗ_тема" not in p
