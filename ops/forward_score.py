# -*- coding: utf-8 -*-
"""ops/forward_score.py — счёт форварда НА ВЗГЛЯДЕ (тезис владельца / механизм библиотеки).

V5 карты v3: каждый §9-вопрос «Шансов недели» несёт thesis_id/mechanism_id; когда исходы
созревают (orchestrator/resolve.py, трек thesis_event), счёт взгляда — попаданий из N —
печатается владельцу в подаче: «этот взгляд пока сбывался в X%».

П16: репутация взгляда рождается ТОЛЬКО из журнала (predictions × outcomes, join по hash).
forward_score в knowledge/mechanisms.yaml — кэш для чтения людьми, не источник истины;
валидатор библиотеки предупреждает о ненулевых значениях в файле. Здесь — единственный
вычислитель. Детерминированно, журналы только читаются."""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRED = ROOT / "journal" / "predictions.jsonl"
OUTC = ROOT / "journal" / "outcomes.jsonl"
KINDS = ("thesis_event",)


def _read_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def view_scores(predictions_path=None, outcomes_path=None):
    """→ {'mechanism:<id>' | 'thesis:<id>': {'вопросов','исходов','попаданий'}}.

    Вопрос с тезисом И механизмом кормит ОБА счёта (взгляды независимы)."""
    preds = [p for p in _read_jsonl(predictions_path or PRED) if p.get("kind") in KINDS]
    outs = {o.get("hash"): o for o in _read_jsonl(outcomes_path or OUTC)
            if o.get("hash") and o.get("outcome") in (0, 1)}
    scores = {}
    for p in preds:
        keys = []
        if p.get("mechanism_id"):
            keys.append(f"mechanism:{p['mechanism_id']}")
        if p.get("thesis_id"):
            keys.append(f"thesis:{p['thesis_id']}")
        o = outs.get(p.get("hash"))
        for k in keys:
            s = scores.setdefault(k, {"вопросов": 0, "исходов": 0, "попаданий": 0})
            s["вопросов"] += 1
            if o is not None:
                s["исходов"] += 1
                s["попаданий"] += int(o["outcome"])
    return scores


def score_phrase(score):
    """Счёт → честные слова для подачи. Нет исходов → «счёт копится», не выдуманный %."""
    s = score or {}
    n, k = int(s.get("исходов") or 0), int(s.get("попаданий") or 0)
    if n <= 0:
        q = int(s.get("вопросов") or 0)
        return (f"счёт взгляда копится — задано {q}, исходов ещё нет" if q
                else "счёт взгляда копится с этих вопросов")
    pct = round(100.0 * k / n)
    base = f"этот взгляд пока сбывался в {pct}% ({k} из {n})"
    if n < 10:
        base += " — выборка мала, доверять рано"
    return base
