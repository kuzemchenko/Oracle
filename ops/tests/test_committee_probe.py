# -*- coding: utf-8 -*-
"""Проверочный прогон комитета (ops/committee_probe.py): всё пишется в свою папку, боевые журналы не трогаются."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import bot_agent as BA          # noqa: E402
import committee_probe as CP    # noqa: E402
import search_queue as SQ       # noqa: E402


def test_probe_runs_in_its_own_folder_and_stops_on_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(BA, "committee_on", lambda: True)
    seen = []

    def answer(q, history, tools):
        seen.append((q, len(history), tools.search_dir, tools.verdict_dir, tools.pred_path))
        s = SQ.active("telegram", tools.search_dir)
        if not s["гипотезы"]:
            SQ.add_hypothesis(s, "Ставки фрахта держатся выше прошлогодних — прибыль танкерной компании растёт",
                              "DHT", "рост ставок фрахта напрямую увеличивает выручку судов на спотовом рынке")
            SQ.save(s, tools.search_dir)
        return "собрал часть доказательств", [], 4.0

    res = CP.run("DHT", "DHT Holdings — нефтяные танкеры", card="reports/ideas/x.card.json", budget=10.0,
                 max_turns=6, out=tmp_path / "probe", answer=answer, log=lambda *a: None)
    assert res["ходов"] == 3 and res["потрачено_usd"] == 12.0           # третий ход перешёл бюджет — стоп
    assert "карточка reports/ideas/x.card.json" in seen[0][0] and seen[1][0].startswith("Продолжай поиск по DHT")
    assert [n for _q, n, *_ in seen] == [0, 2, 4]                       # история разговора копится
    out = tmp_path / "probe"
    assert all(str(p).startswith(str(out)) for _q, _n, *paths in seen for p in paths)
    assert BA.CHAT_LOG.parent == out and BA.CHAT_REQUESTS.parent == out
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert saved["вердикты"][0]["тикер"] == "DHT" and saved["строгость"]["оценено"] == 1
    assert "DHT" in (out / "verdict.txt").read_text(encoding="utf-8")
    assert (out / "transcript.md").read_text(encoding="utf-8").count("## Ход") == 3


def test_probe_survives_a_failed_turn(tmp_path):
    def answer(q, history, tools):
        raise RuntimeError("модель недоступна")

    res = CP.run("DHT", "DHT Holdings", budget=10.0, max_turns=3, out=tmp_path / "probe", answer=answer,
                 log=lambda *a: None)
    assert res["ходов"] == 1 and "сбой" in res["ходы"][0] and (tmp_path / "probe" / "result.json").exists()


def test_probe_model_override_does_not_touch_config_and_has_no_expensive_fallback():
    from orchestrator import openrouter as OR
    before = OR.resolve_role(BA.ROLE)["primary"]
    m = CP.models_with("anthropic/claude-sonnet-4.6")
    cfg = OR.resolve_role(BA.ROLE, m)
    assert cfg["primary"] == "anthropic/claude-sonnet-4.6" and cfg["fallbacks"] == []
    assert OR.filtered_chain(cfg, None, m) == ["anthropic/claude-sonnet-4.6"]
    assert OR.resolve_role(BA.ROLE)["primary"] == before                  # настройки владельца не тронуты
