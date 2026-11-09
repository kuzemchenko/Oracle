# -*- coding: utf-8 -*-
"""VQ-4: актуальный статус идеи отдельно от истории прогноза (ops/idea_revision.current).

Приёмка: цена пересекла условие отмены → карточка не выглядит актуальной; катализатор состоялся →
проверяется результат, а не повторяется ожидание; исходные прогнозы и их результаты после пересмотра
не изменены; устаревшая цена → план не действует."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from ops import idea_revision as IR   # noqa: E402

CARD = ROOT / "reports" / "ideas" / "idea_2026-11-02_MP.card.json"   # вход 40.3, цель 47.5, стоп 35.5,
                                                                    # катализатор 2026-11-10, срок 2026-11-25


def _stem(tmp_path):
    (tmp_path / "idea_2026-11-02_MP.card.json").write_text(CARD.read_text("utf-8"), encoding="utf-8")
    return str(tmp_path / "idea_2026-11-02_MP")


def test_active_when_nothing_triggered(tmp_path):
    cur = IR.current(_stem(tmp_path), closes=[("2026-11-03", 40.0), ("2026-11-06", 41.0)], today="2026-11-07",
                     out_dir=tmp_path / "rev")
    assert cur["статус"] == "действует" and cur["план_действует"]


def test_stop_crossed_makes_card_not_actual(tmp_path):
    stem = _stem(tmp_path)
    card = json.loads(CARD.read_text("utf-8"))
    cur = IR.current(stem, closes=[("2026-11-03", 40.0), ("2026-11-05", 33.74)], today="2026-11-07",
                     out_dir=tmp_path / "rev", persist=True)
    assert cur["статус"] == "отменена условием" and not cur["план_действует"]
    lines = IR.plan_or_archive(card, cur, ["Вход. Не дороже $40.30"])
    assert "НЕ действует" in lines[0] and "Архивные уровни" in lines[1] and "Вход. Не дороже" not in " ".join(lines)
    hist = json.loads(IR.status_path(stem, tmp_path / "rev").read_text("utf-8"))["история"]
    assert hist[-1]["статус"] == "отменена условием" and hist[-1]["источник"] == "перепроверка кода"


def test_catalyst_passed_requires_result_not_waiting(tmp_path):
    stem = _stem(tmp_path)
    cur = IR.current(stem, closes=[("2026-11-10", 40.0), ("2026-11-11", 40.5)], today="2026-11-12",
                     out_dir=tmp_path / "rev")
    assert cur["статус"] == "проверить результат катализатора" and not cur["план_действует"]
    assert any("2026-11-10" in q for q in cur["вопросы"])
    # результат катализатора проверен и записан → снова «действует» (или другое решение с основанием)
    IR.set_status(stem, "действует", ["отчёт 10.11: выручка $X (8-K, sec.gov), тезис подтверждён"],
                  source="катализатор", date="2026-11-12", out_dir=tmp_path / "rev")
    cur2 = IR.current(stem, closes=[("2026-11-11", 40.5)], today="2026-11-12", out_dir=tmp_path / "rev")
    assert cur2["статус"] == "действует"


def test_stale_price_and_expired_term(tmp_path):
    stem = _stem(tmp_path)
    cur = IR.current(stem, closes=[("2026-11-03", 40.0)], today="2026-11-12", out_dir=tmp_path / "rev")
    assert "устарела" in cur["перепроверка"]
    cur = IR.current(stem, closes=[("2026-11-26", 40.0)], today="2026-11-27", out_dir=tmp_path / "rev")
    assert cur["статус"] == "срок истёк"


def test_refuted_status_dominates_and_original_untouched(tmp_path):
    stem = _stem(tmp_path)
    before = (tmp_path / "idea_2026-11-02_MP.card.json").read_bytes()
    IR.set_status(stem, "отклонена", ["прокачка, день 1: РАЗБИТА"], source="прокачка, день 1",
                  date="2026-11-07", out_dir=tmp_path / "rev")
    cur = IR.current(stem, closes=[("2026-11-06", 41.0)], today="2026-11-07", out_dir=tmp_path / "rev")
    assert cur["статус"] == "отклонена" and not cur["план_действует"]
    assert (tmp_path / "idea_2026-11-02_MP.card.json").read_bytes() == before   # прогноз не тронут
    try:
        IR.set_status(stem, "действует", [], out_dir=tmp_path / "rev")
        raise AssertionError("статус без основания")
    except ValueError:
        pass


def test_status_layer_does_not_touch_sealed_records_or_outcomes(tmp_path):
    """Исходный прогноз и его итог считаются по исходным условиям: статус их не меняет."""
    from mathlib import idea_outcome as IO
    card = json.loads(CARD.read_text("utf-8"))
    closes = [("2026-11-03", 40.0), ("2026-11-05", 33.74), ("2026-11-20", 48.0)]
    before = IO.evaluate(card, dict(closes), "2026-11-02")
    stem = _stem(tmp_path)
    IR.current(stem, closes=closes, today="2026-11-21", out_dir=tmp_path / "rev", persist=True)
    assert IO.evaluate(card, dict(closes), "2026-11-02") == before
    assert IR.verify() == []                              # боевые исходные карточки не менялись
