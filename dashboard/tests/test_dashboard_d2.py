# -*- coding: utf-8 -*-
"""Д2 — табло §15 с ДВУМЯ рядами калибровки (решение владельца 13.07, Вопрос 2).

Официальный ряд (из outcomes.jsonl) ПЕРВИЧЕН и не меняется; диагностический ряд читается
ТОЛЬКО из ops/reports/d2_diagnosis/report.json. Файла нет → прежнее поведение (в метриках
нет нового ключа, в HTML нет пометки Д2) — «байт-в-байт»: код нового ряда весь за проверкой
существования файла. Битый файл → как отсутствующий (fail-quiet в прежнее поведение)."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dashboard import build_dashboard as DB  # noqa: E402

D2_ROW = {"n": 252, "hit_rate": 0.3611, "brier": 0.2357,
          "пометка": "диагностический ряд Д2 (пересчёт из сырых котировок)"}
D2_REPORT = {"dashboard_row": D2_ROW, "вердикт": {"баг_сверки_подтверждён": False}}


def test_no_report_no_diagnostic_row(monkeypatch, tmp_path):
    monkeypatch.setattr(DB, "D2_DIAGNOSIS_JSON", tmp_path / "нет_файла.json")
    m = DB.metric_calibration()
    assert "диагностический_ряд_д2" not in m          # прежнее поведение: ключа просто нет
    html = DB._calibration_card(m)
    assert "Диагностический ряд" not in html
    assert "Д2" not in html


def test_report_present_adds_second_row_official_untouched(monkeypatch, tmp_path):
    p = tmp_path / "report.json"
    p.write_text(json.dumps(D2_REPORT, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(DB, "D2_DIAGNOSIS_JSON", p)
    m = DB.metric_calibration()
    d2 = m.get("диагностический_ряд_д2")
    assert d2 is not None
    assert d2["n"] == 252 and d2["hit_rate"] == 0.3611 and d2["brier"] == 0.2357
    assert d2["баг_сверки_подтверждён"] is False
    assert "официальный ряд" in d2["статус"]
    # официальный ряд не подменён: его поля прежние и живут отдельно от диагностического
    monkeypatch.setattr(DB, "D2_DIAGNOSIS_JSON", tmp_path / "нет.json")
    m_off = DB.metric_calibration()
    assert {k: v for k, v in m.items() if k != "диагностический_ряд_д2"} == m_off
    # HTML (Д2 #17): при НЕподтверждённом баге заголовок НЕ говорит «после найденной ошибки»
    html = DB._calibration_card(m)
    assert "после найденной ошибки" not in html
    assert "баг сверки НЕ подтверждён" in html
    assert "официальный ряд выше" in html
    assert "корзина" in html


def test_d2_card_heading_honest_by_verdict(monkeypatch, tmp_path):
    """Д2 #17: заголовок диагностического ряда честен относительно вердикта бага."""
    p = tmp_path / "report.json"
    # баг ПОДТВЕРЖДЁН → допустимо «после найденной ошибки Д2»
    p.write_text(json.dumps({"dashboard_row": D2_ROW,
                             "вердикт": {"баг_сверки_подтверждён": True}}), encoding="utf-8")
    monkeypatch.setattr(DB, "D2_DIAGNOSIS_JSON", p)
    html = DB._calibration_card(DB.metric_calibration())
    assert "после найденной ошибки Д2" in html
    assert "подтверждён: <b>ДА</b>" in html


def test_official_row_uses_recorded_outcome_not_recomputed(monkeypatch, tmp_path):
    """Д2 #13 (кросс-ревью): официальный ряд берёт outcome КАК ЗАПИСАНО в outcomes.jsonl, а не
    пересчитывает через resolve_prediction. Контрпример: журнал говорит outcome=0, но
    observed_value>threshold (пересчёт дал бы 1) — официальный ряд обязан показать 0."""
    monkeypatch.setattr(DB, "D2_DIAGNOSIS_JSON", tmp_path / "нет.json")   # без диагностического ряда
    preds = [{"hash": "h1", "probability": 0.9, "threshold": 100.0, "direction": "above",
              "kind": "calibration"}]
    outs = {"h1": {"hash": "h1", "outcome": 0, "observed_value": 105.0,   # >порог: пересчёт дал бы 1
                   "observed_at": "2026-06-30T20:00:00+00:00", "resolve_by": "2026-06-29T20:00:00+00:00",
                   "threshold": 100.0, "direction": "above", "probability": 0.9}}
    monkeypatch.setattr(DB.sealing, "read_predictions", lambda *a, **k: preds)
    monkeypatch.setattr(DB.RES, "outcomes_by_hash", lambda *a, **k: outs)
    m = DB.metric_calibration()
    assert m["n_разрешённых"] == 1
    # Brier по ЗАПИСАННОМУ исходу 0: (0.9-0)^2 = 0.81; по пересчитанному 1 было бы 0.01
    assert m["brier"] == 0.81


def test_corrupted_or_incomplete_report_ignored(monkeypatch, tmp_path):
    p = tmp_path / "report.json"
    p.write_text("{битый json", encoding="utf-8")
    monkeypatch.setattr(DB, "D2_DIAGNOSIS_JSON", p)
    assert DB._d2_diagnostic_row() is None
    p.write_text(json.dumps({"вердикт": {}}), encoding="utf-8")   # нет dashboard_row
    assert DB._d2_diagnostic_row() is None
    p.write_text(json.dumps({"dashboard_row": {"hit_rate": 0.4}}), encoding="utf-8")  # нет n
    assert DB._d2_diagnostic_row() is None
