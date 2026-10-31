# -*- coding: utf-8 -*-
"""A2 (разворот А+B, «делай сразу все» 15.07): датчик толпы (Google Trends → attention.py) и проверяемый
§9-прогноз как ДЕТЕРМИНИРОВАННЫЕ опоры судьи для критериев crowd_fresh и falsifiability (не глазомер).
Проверяем чистые строчные хелперы event_first (без LLM/сети):
  • _crowd_signal: ok → свежесть+фаза+директива; «не измерено» → НЕЙТРАЛЬНО (не штраф); мусор → None;
  • _falsifiable_forecast: полный что/сколько/срок → «высокий»; неполный → «ограничена».
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_first as EF          # noqa: E402


def test_crowd_signal_ok_early_phase_measurement_only():
    """stage-review 31.10: датчик даёт ИЗМЕРЕНИЕ (свежесть, фаза), балл не предрешает (нет «ВЫСОКИЙ/НИЗКИЙ»)."""
    f = {"статус": "ok", "свежесть": 0.72, "фаза": "РАНО"}
    s = EF._crowd_signal(f)
    assert "72%" in s and "РАНО" in s and "Trends" in s
    assert "ВЫСОКИЙ" not in s and "НИЗКИЙ" not in s


def test_crowd_signal_trap_phase_speaks_attention_not_price():
    """stage-review 31.10 №3: датчик видит только ряд интереса — про цену не говорит («цена уехала» — П8-выдумка)."""
    s = EF._crowd_signal({"статус": "ok", "свежесть": 0.15, "фаза": "ЛОВУШКА"})
    assert "15%" in s and "ЛОВУШКА" in s and "вниман" in s.lower()
    assert "цена уехала" not in s.lower() and "НИЗКИЙ" not in s


def test_crowd_signal_not_measured_is_neutral_not_penalty():
    s = EF._crowd_signal({"статус": "не_измерено", "причина": "нет ряда"})
    low = s.lower()
    assert "не измерено" in low and "нейтрально" in low
    assert "не штраф" in low          # ключевое: отсутствие данных не казнит crowd_fresh


def test_crowd_signal_garbage_none():
    assert EF._crowd_signal(None) is None
    assert EF._crowd_signal("x") is None


def test_falsifiable_forecast_magnitude_from_amplitude_not_full():
    """stage-review 31.10 №2: знак И величина — из amplitude (то, что запечатывается); полный ход — отдельно,
    со знаком; «высокий» из кода не ставится (балл — дело судьи); величина §9 не сверяется — только знак."""
    n = {"amplitude": 0.03, "edge_total": 0.05, "horizon_days": 5}
    s = EF._falsifiable_forecast(n)
    assert "рост" in s and "3.0%" in s and "+5.0%" in s and "5 торг" in s
    assert "высокий" not in s.lower() and "ТОЛЬКО по знаку" in s


def test_falsifiable_forecast_overplayed_node_sign_conflict_is_flagged():
    """amplitude −2% при полном +3% (переотыгранный узел): раньше судье уходило «снижение ≈ 3.0%» —
    прогноз, которого движок не делал. Теперь: снижение ≈ 2.0%, полный +3.0% и явная пометка конфликта."""
    s = EF._falsifiable_forecast({"amplitude": -0.02, "edge_total": 0.03, "horizon_days": 5})
    assert "снижение" in s and "2.0%" in s and "+3.0%" in s
    assert "3.0%" not in s.split("полный")[0]          # величина 3.0 не приписана направлению
    assert "ВНИМАНИЕ" in s and "противоположен" in s


def test_falsifiable_forecast_short_direction():
    n = {"amplitude": -0.04, "edge_total": -0.04, "horizon_days": 3}
    s = EF._falsifiable_forecast(n)
    assert "снижение" in s and "4.0%" in s and "3 торг" in s and "ВНИМАНИЕ" not in s


def test_falsifiable_forecast_incomplete_is_limited():
    n = {"amplitude": 0.03, "edge_total": 0.03}     # нет срока
    s = EF._falsifiable_forecast(n)
    assert "ограничена" in s.lower()


def test_falsifiable_forecast_empty_honest():
    s = EF._falsifiable_forecast({})
    assert "не полны" in s.lower() or "ниже" in s.lower()
