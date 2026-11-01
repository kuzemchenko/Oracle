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
         "lag_weeks": "2-8", "watch_assets": "нефть", "precedents": [{"year": "2022", "observed": "+35%"}]},
    ]}, ensure_ascii=False), encoding="utf-8")
    return p


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
    assert sum("дубль в журнале" in b["причина"] for b in r2["брак"]) == 2
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
    assert "USO.US" in text and "нефть" in text.lower()          # тикер с именем


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
    # флаг включён → один пуш, повтор — дедуп
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"week_odds_push": True})
    b._tick_week_odds()
    assert len(b.tg.sent) == 1 and "Шансы недели" in b.tg.sent[0]
    b._tick_week_odds()
    assert len(b.tg.sent) == 1
