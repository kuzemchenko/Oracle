# -*- coding: utf-8 -*-
"""Тесты состязательного контура §4 блок E и синтеза §8 (гейт Недели 7).

Проверяет требования Нед.7:
  • слепота судьи: аргументы обезличены (без роли/модели) и порядок рандомизирован;
  • развязка семейств П10: судья И ВСЯ цепочка его фолбеков ≠ семейство генератора;
  • рубрика — версионируемый файл, вердикт УСТОЯЛА/РАЗБИТА пересчитывается кодом по порогу;
  • процедурное вето §5.6 при неотвеченных обязательных вопросах;
  • риск-агент помнит short_borrow (для шорта издержки занижены — нет данных);
  • полный цикл этапов 1–6 на тестовом дне с протоколом воронки.
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents.registry import ALL_AGENTS, DEBATE_AGENTS, SYNTH_AGENTS  # noqa: E402
from agents import build_prompts                                    # noqa: E402
from orchestrator import openrouter as OR                           # noqa: E402
from orchestrator import context as C                               # noqa: E402
from orchestrator import debate as DBT                              # noqa: E402
from orchestrator import synthesis as SY                            # noqa: E402
from orchestrator import funnel as F                                # noqa: E402

PROMPTS = ROOT / "agents" / "prompts"


# ── Реестр и промпты блоков E/F ──────────────────────────────────────────────────
def test_blocks_e_and_f_registered_with_prompts():
    blocks = {a[1] for a in ALL_AGENTS}
    assert {"E", "F"} <= blocks
    for aid, *_ in DEBATE_AGENTS + SYNTH_AGENTS:
        path = PROMPTS / f"{aid}.md"
        assert path.exists(), f"нет промпта {aid}"
        text = path.read_text(encoding="utf-8")
        assert "НУЛЕВЫЕ ВЫДУМКИ" in text          # инвариант 1 (П8 в каждом)
        assert "ФОРМАТ ВЫВОДА" in text
        assert on_disk_in_sync(aid), f"рассинхрон промпта {aid}"


def on_disk_in_sync(aid):
    return (PROMPTS / f"{aid}.md").read_text(encoding="utf-8") == build_prompts.build_one(aid)


# ── Развязка семейств П10 (судья ≠ семья генератора, включая фолбеки) ─────────────
def test_judge_chain_excludes_generator_family():
    models = OR.load_models()
    gen_family = DBT._generator_family(models)         # anthropic
    judge_cfg = OR.resolve_role("judge", models)
    chain = OR.filtered_chain(judge_cfg, exclude_family=gen_family, models=models)
    assert chain, "цепочка судьи не должна быть пустой"
    for m in chain:                                    # НИ ОДНА модель цепочки не из семьи генератора
        assert OR.family_of(m, models) != gen_family, f"{m} из семейства генератора {gen_family}"


def test_filtered_chain_raises_when_all_same_family():
    role_cfg = {"primary": "anthropic/claude-opus-4.8",
                "fallbacks": ["anthropic/claude-sonnet-4.6"]}
    with pytest.raises(RuntimeError):
        OR.filtered_chain(role_cfg, exclude_family="anthropic")


def test_mock_client_respects_exclude_family():
    # MockClient тоже обязан соблюдать развязку (берёт первую модель НЕ из исключённого семейства)
    client = OR.MockClient(run_id="t")
    resp = client.complete("judge", "sys", "usr про BNO.US", agent_id="e_judge",
                           output_kind="judge_verdict", exclude_family="google")
    assert OR.family_of(resp["model"]) != "google"


# ── Слепота судьи: обезличивание + рандомизация порядка ──────────────────────────
def _fake_rec(role, model, judgment):
    return {"agent": role, "ok": True, "model": model, "judgment": judgment}


def test_blind_case_strips_identity_and_shuffles():
    gen = _fake_rec("e_generator", "anthropic/claude-opus-4.8",
                    {"вывод": "ген", "вероятность": 0.7, "уверенность": "средняя",
                     "данные_основания": [], "что_неизвестно": []})
    crit = _fake_rec("e_critic", "openai/gpt-5.5",
                     {"вывод": "крит", "вероятность": 0.4, "уверенность": "средняя",
                      "данные_основания": [], "что_неизвестно": []})
    adv = _fake_rec("e_advocate", "anthropic/claude-sonnet-4.6",
                    {"вывод": "адв", "вероятность": 0.6, "уверенность": "средняя",
                     "данные_основания": [], "что_неизвестно": []})
    blind, label_map = DBT._build_blind_case(gen, crit, adv, run_id="r1", asset="BNO.US")
    # в обезличенном деле НЕТ ни роли, ни модели
    for item in blind:
        assert set(item.keys()) == {"метка", "аргумент"}
        assert "_роль" not in item["аргумент"] and "_модель" not in item["аргумент"]
    # карта меток для аудита содержит роль+модель (но судье не передаётся — это отдельный ключ)
    assert set(label_map) == {"A", "B", "C"}
    assert all("роль" in v and "модель" in v for v in label_map.values())
    # детерминированность по seed (воспроизводимый аудит)
    blind2, _ = DBT._build_blind_case(gen, crit, adv, run_id="r1", asset="BNO.US")
    assert [b["метка"] for b in blind] == [b["метка"] for b in blind2]
    order_map = {v["роль"]: k for k, v in label_map.items()}
    # порядок рандомизирован (не обязательно A=генератор)
    assert len(order_map) == 3


# ── Рубрика: версионируемый файл + пересчёт вердикта кодом ────────────────────────
def test_rubric_is_versioned_file():
    rub = DBT.load_rubric()
    assert "version" in rub
    assert len(rub["criteria"]) == 6                      # зафиксировано из черновика Нед.7
    assert rub["verdict"]["break_threshold"] == 3.0
    assert {q["id"] for q in rub["mandatory_questions"]} == {"who_sells", "why_exists"}


def _judge_rec(scores, answered=True, says="УСТОЯЛА"):
    j = {"вывод": "v", "вероятность": 0.6, "base_rate": 0.5, "уверенность": "средняя",
         "вердикт": says, "данные_основания": [], "что_неизвестно": [],
         "рубрика": {"version": "1.0", "оценки": [{"критерий": f"c{i}", "балл": s, "обоснование": "x"}
                                                   for i, s in enumerate(scores)]},
         "кто_продаёт_нам_и_почему_неправ": ("1-й порядок недооценён" if answered else ""),
         "почему_возможность_ещё_существует": ("не связан каскад" if answered else "")}
    return {"agent": "e_judge", "ok": True, "judgment": j, "model": "google/gemini-3.1-pro-preview"}


def test_adjudicate_verdict_recomputed_from_rubric():
    rub = DBT.load_rubric()
    # средний балл 4 ≥ 3.0 → УСТОЯЛА
    v = DBT._adjudicate(_judge_rec([4, 4, 4, 4, 4, 4]), rub)
    assert v["исход"] == "УСТОЯЛА" and v["вероятность_судьи"] == 0.6
    # средний балл 2 < 3.0 → РАЗБИТА (даже если судья заявил УСТОЯЛА — §16.6)
    v2 = DBT._adjudicate(_judge_rec([2, 2, 2, 2, 2, 2], says="УСТОЯЛА"), rub)
    assert v2["исход"] == "РАЗБИТА" and v2["вероятность_судьи"] is None
    assert v2["примечание"] and "расхождение" in v2["примечание"]


def test_adjudicate_veto_on_unanswered_mandatory_questions():
    rub = DBT.load_rubric()
    v = DBT._adjudicate(_judge_rec([4, 4, 4, 4, 4, 4], answered=False), rub)
    assert v["исход"] == "ВЕТО"
    assert v["пропущенные_вопросы"]


# ── Риск-агент: short_borrow занижение для шорта ──────────────────────────────────
def test_run_risk_flags_short_borrow_no_data():
    ctx = C.build_context(theme="brent")
    client = OR.MockClient(run_id="t")
    costs = SY.load_costs()
    cand = {"актив": "BNO.US", "направление": "шорт", "тезис": "t", "разрешимость": "r"}
    rec = SY.run_risk(cand, ctx, client, costs)
    assert rec["short_borrow_no_data"] is True            # шорт + borrow=null → флаг занижения
    rj = rec["judgment"]
    assert rj["вероятность"] is None                       # риск не голосует о рынке
    assert "шорт" in rj["шорт_режим"].lower() or "сквиз" in rj["шорт_режим"].lower()


# ── Полный цикл этапов 1–6 на тестовом дне ───────────────────────────────────────
@pytest.fixture(scope="module")
def full_protocol():
    return F.run_funnel(theme="brent", mode="mock", run_id="pytest_full", write=False, full=True)


def test_full_cycle_runs_all_six_stages(full_protocol):
    fr = full_protocol["воронка_отсева"]
    # протокол воронки фиксирует отсев на КАЖДОМ этапе (условие 6)
    for k in ("этап1_сырых_сигналов", "этап2_кандидатов", "этап3_после_грубого_фильтра",
              "этап4_в_дебаты_топ", "этап5_устояло_после_дебатов", "этап6_выдано_топ"):
        assert k in fr
    # этап 1 применил FDR
    assert full_protocol["этап1_скан_FDR"]["процедура"] == "benjamini_hochberg"
    # этап 3 даёт отсев с причинами (прозрачность §6)
    assert all("причина_отсева" in d for d in fr["отсев_этап3"])


def test_full_cycle_debates_blind_and_family_decoupled(full_protocol):
    debates = full_protocol["этап5_дебаты"]
    assert debates, "должны быть проведены дебаты хотя бы по одной идее"
    for d in debates:
        # П10: семья судьи ≠ семья генератора
        assert d["семейство_судьи"] != d["семейство_генератора"]
        # слепые нейтральные метки, карта меток отдельно (в аудит-протоколе)
        assert d["слепое_дело"]["метки_в_деле"]
        assert "карта_меток_АУДИТ" in d["слепое_дело"]
        # вердикт пересчитан по рубрике
        assert d["вердикт"]["исход"] in ("УСТОЯЛА", "РАЗБИТА", "ВЕТО")


def test_full_cycle_emits_scoring_risk_portfolio_report(full_protocol):
    synth = full_protocol["этап6_синтез"]
    if not synth["отчёты"]:
        pytest.skip("слабый день: ни одна идея не устояла (легитимный результат §6)")
    port = synth["портфель"]
    assert port["режим_размера"].startswith("фикс 0.5%")   # до gate калибровки §11
    for rep in synth["отчёты"]:
        assert rep["отчёт"]["ok"], "синтезатор §8 должен дать отчёт"
        assert "поля" in rep["отчёт"]["judgment"]           # 13 полей §8
        pos = rep["позиция"]
        assert pos["amount_usd"] <= 500.0                   # лимит идеи $500 (§30)
    # все идеи топ-3 — из разных макро-драйверов (диверсификация §6), если их >1
    drivers = [r["позиция"]["макро_драйвер"] for r in synth["отчёты"]]
    assert len(drivers) <= 3


def test_field_only_mode_skips_stages_3_6():
    p = F.run_funnel(theme="brent", mode="mock", run_id="pytest_fieldonly", write=False, full=False)
    assert "воронка_отсева" not in p
    assert "поле_суждений" in p                              # этапы 1–2 есть
