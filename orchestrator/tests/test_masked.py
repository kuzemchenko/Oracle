# -*- coding: utf-8 -*-
"""Тесты харнесса маскированных кейсов §23.2(б) (orchestrator/masked).

Главное требование честности (инвариант 3 CLAUDE.md, П16): эталон_АУДИТ — ожидаемое
направление, expected_stance, подсказка об оригинале — НИКОГДА не попадает к агентам.
Плюс: набор смешанный (affirm+reject); mock — дымовой тест конвейера, не доказательный гейт."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import masked as M  # noqa: E402

CASES = M.load_cases()
AUDIT_LEAK_MARKERS = ["эталон_АУДИТ", "expected_stance", "ожидаемое_направление",
                      "оригинал_подсказка", "ожидаемое_направление", "каскад"]


def test_cases_present():
    assert len(CASES) >= 6


def test_every_case_has_stance_and_imperfection():
    for c in CASES:
        assert M._expected_stance(c) in ("affirm", "reject")
        assert "несовершенство_маскировки" in c, f"{c['id']}: нет пометки несовершенства маскировки"


def test_mixed_set():
    stances = [M._expected_stance(c) for c in CASES]
    assert "affirm" in stances and "reject" in stances, "набор обязан быть смешанным"


def test_agent_payload_hides_audit():
    """_agent_payload не должен содержать НИ ОДНОГО поля аудит-блока (защита от утечки ответа).

    Проверяем по ИМЕНАМ аудит-полей (а не широким словам вроде «каскад», которые легитимно
    встречаются в нейтральной ситуации — напр. «макро-каскад»). Структурно _agent_payload берёт
    строго `подаётся_агентам`, поэтому аудит туда попасть не может — это belt-and-suspenders."""
    import json
    AUDIT_FIELD_NAMES = ("expected_stance", "ожидаемое_направление", "оригинал_подсказка",
                         "эталон_АУДИТ", "сигналы_хорошего_рассуждения", "что_должно_быть_неизвестно",
                         "кто_продаёт")
    for c in CASES:
        payload = M._agent_payload(c)
        # ни один ключ аудит-блока не должен присутствовать как поле payload (на любом уровне)
        blob = json.dumps(payload, ensure_ascii=False)
        for marker in AUDIT_FIELD_NAMES:
            assert marker not in blob, f"{c['id']}: утечка аудит-поля '{marker}' в payload агентов!"
        # payload обязан нести именно нейтральное содержимое
        assert "нейтральный_тикер" in payload
        assert "ситуация" in payload


def test_candidate_hides_direction():
    """В кандидате, идущем в контур, направление и разрешимость СКРЫТЫ (контур решает сам)."""
    for c in CASES:
        cand = M._build_candidate(c)
        assert cand["направление"] is None
        assert cand["разрешимость"] is None
        # тезис — это маскированная ситуация, без подсказки направления
        stance_words = ["лонг", "шорт", "expected", "affirm", "reject"]
        # ситуация может содержать слово, но не ожидаемое_направление аудита:
        assert c["эталон_АУДИТ"].get("ожидаемое_направление") != cand["тезис"]


def test_ctx_carries_only_neutral_indicators():
    for c in CASES:
        ctx, costs = M._build_ctx(c)
        tkr = M._agent_payload(c)["нейтральный_тикер"]
        assert tkr.startswith("MASK_")
        assert tkr in costs


def test_mock_run_is_pipeline_smoke_not_gate():
    s = M.run_masked(mode="mock", write=False)
    assert s["mode"] == "mock"
    assert "MOCK" in s["честность"]
    # все 6 кейсов оценены, П8 чисто, оба вопроса отвечены (mock-судья даёт по форме)
    assert s["агрегат"]["n_кейсов"] == len(CASES)
    assert s["агрегат"]["n_чисто_П8"] == len(CASES)
    # каждый результат помечен несовершенством маскировки
    assert all(r["маскировка_несовершенна"] for r in s["кейсы"])


def test_aggregate_has_imperfection_caveat():
    s = M.run_masked(mode="mock", write=False)
    assert "несовершенна" in s["агрегат"]["оговорка"]
