# -*- coding: utf-8 -*-
"""orchestrator/ablation.py — агрегатор абляции вкладов агентов (MASTER_SPEC §11.1).

§11.1: раз все агенты живут сразу, «кто реально приносит пользу, а кто шумит» решается
абляцией, а не поэтапным A/B-вводом. Механизм состоит из ДВУХ частей:

  1) КОНТРФАКТЫ НА КАЖДОМ СИНТЕЗЕ (считаются уже сейчас, на тестовых прогонах).
     Дирижёр на синтезе сохраняет drop-one: какой была бы агрегированная вероятность БЕЗ
     голоса агента X (orchestrator/funnel.counterfactual_protocol — дёшево, это математика,
     не повторные LLM-вызовы). Эти числа лежат в journal/funnel_logs/*.json. Здесь мы их
     ЧИТАЕМ и агрегируем в таблицу влияния: сколько раз агент голосовал и насколько его голос
     двигал агрегат (|сдвиг|). Это работает БЕЗ единого разрешённого исхода — и доказывает,
     что контрфакты по прогонам реально считаются.

  2) ВКЛАД В BRIER (форвард, ежемесячно). Когда форвард-исход разрешается (0/1), вклад агента
     X = Brier(P_без_X, исход) − Brier(P_агрегат, исход). Положительная дельта ⟹ удаление X
     ухудшило Brier ⟹ X ПОМОГАЛ; отрицательная ⟹ X шумел. По правилам §10 (N≥30, значимость)
     устойчиво отрицательный вклад → ПРЕДЛОЖЕНИЕ понизить вес вплоть до карантина (НЕ удаление:
     суждения агента продолжают журналироваться, как у агента примет). Применение — только
     через /apply-weights (ежемесячно). Пока разрешённых исходов < 30 — честно «накапливается».

Детерминированный код (инвариант 6): и drop-one (в funnel), и Brier-дельта здесь — математика
на журналах, не LLM. Выход — journal/proposed_adjustments.md (ПРЕДЛОЖЕНИЯ) + сводка.
"""
import json
import pathlib
import datetime

import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import brier as B          # noqa: E402
from mathlib import sealing             # noqa: E402
from mathlib import outcomes as OUT     # noqa: E402
from orchestrator import resolve as RES  # noqa: E402  (join прогноз↔исход по hash, ревью 04.07 H1)

FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"
PROPOSED = ROOT / "journal" / "proposed_adjustments.md"
MIN_N = 30                              # §10: значимость только при N≥30


# ── Чтение контрфактических протоколов из логов прогонов ─────────────────────────
def load_run_counterfactuals(funnel_logs=FUNNEL_LOGS):
    """Список прогонов с их drop-one контрфактами (из journal/funnel_logs/*.json).

    Возвращает по прогону: run_id, ts, mode, агрегированная_вероятность и
    {agent: {вероятность_без_него, сдвиг}}."""
    runs = []
    for p in sorted(pathlib.Path(funnel_logs).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        cf = d.get("контрфактический_протокол")
        if not cf or not cf.get("контрфакты"):
            continue
        per_agent = {c["без_агента"]: {"вероятность_без_него": c.get("вероятность_без_него"),
                                       "сдвиг": c.get("сдвиг")}
                     for c in cf["контрфакты"]}
        runs.append({
            "run_id": d.get("run_id", p.stem),
            "ts": d.get("ts"),
            "mode": d.get("mode"),
            "theme": d.get("theme"),
            "агрегированная_вероятность": cf.get("агрегированная_вероятность"),
            "n_голосов": cf.get("n_голосов"),
            "контрфакты": per_agent,
        })
    return runs


# ── Часть 1: таблица влияния (работает без исходов) ──────────────────────────────
def influence_table(runs):
    """Агент → сколько прогонов участвовал и насколько его голос двигал агрегат.

    mean_abs_shift — средняя |величина| сдвига агрегата при удалении агента (сколько «весит»
    его голос в синтезе). mean_shift — средний знаковый сдвиг (тянул агрегат вверх/вниз).
    Это НЕ оценка качества (для неё нужны исходы) — это информативность голоса."""
    acc = {}
    for r in runs:
        for agent, c in r["контрфакты"].items():
            sh = c.get("сдвиг")
            a = acc.setdefault(agent, {"n": 0, "shifts": []})
            a["n"] += 1
            if isinstance(sh, (int, float)):
                a["shifts"].append(float(sh))
    table = []
    for agent, a in sorted(acc.items()):
        sh = a["shifts"]
        table.append({
            "agent": agent,
            "n_участий": a["n"],
            "mean_abs_shift": round(sum(abs(x) for x in sh) / len(sh), 5) if sh else None,
            "mean_shift": round(sum(sh) / len(sh), 5) if sh else None,
        })
    table.sort(key=lambda x: (x["mean_abs_shift"] or 0), reverse=True)
    return table


# ── Часть 2: вклад в Brier (нужны разрешённые форвард-исходы) ─────────────────────
def brier_delta(p_agg, p_without, outcome):
    """Вклад одного голоса в одном исходе: Brier(без X) − Brier(агрегат).

    >0 ⟹ без X хуже ⟹ X помогал; <0 ⟹ X шумел. Чистая математика."""
    return B.brier_score([p_without], [outcome]) - B.brier_score([p_agg], [outcome])


def agent_brier_contribution(linked):
    """linked: список записей {agent, p_agg, p_without, outcome}. Группирует по агенту →
    средняя Brier-дельта, N, и значимость по §10 (N≥30 и знак устойчив).

    Пустой вход (нет разрешённых исходов) → пустая таблица: НЕ выдумываем вклад (П8)."""
    by = {}
    for rec in linked:
        by.setdefault(rec["agent"], []).append(
            brier_delta(rec["p_agg"], rec["p_without"], rec["outcome"]))
    out = []
    for agent, deltas in sorted(by.items()):
        n = len(deltas)
        mean = sum(deltas) / n if n else None
        n_pos = sum(1 for d in deltas if d > 0)
        # «помогал» (mean_delta>0) считаем значимым только при N≥30 и устойчивом знаке (>2/3)
        significant = (n >= MIN_N) and (max(n_pos, n - n_pos) / n >= 2 / 3)
        verdict = "накапливается (N<30)" if n < MIN_N else (
            ("вклад ПОЛОЖИТЕЛЬНЫЙ (помогает)" if mean and mean > 0 else
             "вклад ОТРИЦАТЕЛЬНЫЙ → предложить понижение веса/карантин") if significant
            else "знак неустойчив — не значимо")
        out.append({
            "agent": agent, "n_исходов": n,
            "mean_brier_delta": round(mean, 6) if mean is not None else None,
            "n_помогал": n_pos, "значимо_§10": significant, "вывод": verdict,
        })
    out.sort(key=lambda x: (x["mean_brier_delta"] if x["mean_brier_delta"] is not None else 0))
    return out


def load_resolved_links(predictions_path=None):
    """Связки {agent, p_agg, p_without, outcome} из разрешённых форвард-прогнозов.

    Прогноз обязан нести run_id (привязка к контрфактам прогона) и быть разрешённым по §10.
    Пока форвард-исходов нет — возвращает [] (НЕ выдумываем; абляция честно «накапливается»).

    NB: сверку цены делает mathlib.outcomes детерминированно; здесь только склейка. Текущий
    этап (Нед.8, до Бумаги) разрешённых исходов не имеет — это ожидаемое «нет данных» (П8)."""
    recs = sealing.read_predictions(predictions_path)
    runs = {r["run_id"]: r for r in load_run_counterfactuals()}
    # ревью 04.07 H1: исходы — в outcomes.jsonl (join по hash); в predictions их нет и не бывает,
    # без join часть 2 абляции («вклад агента в Brier», петля §25) не наступила бы никогда
    outs_map = RES.outcomes_by_hash()
    linked = []
    for pred in recs:
        if pred.get("tag") == "test":
            continue                                   # демо-записи запечатывания — не прогноз
        run_id = pred.get("run_id")
        o = outs_map.get(pred.get("hash")) or {}
        res = OUT.resolve_prediction(pred, o.get("observed_value"), o.get("observed_at"))
        if res["status"] != "resolved" or run_id not in runs:
            continue
        agent_cf = runs[run_id]["контрфакты"]
        p_agg = runs[run_id]["агрегированная_вероятность"]
        for agent, c in agent_cf.items():
            if c.get("вероятность_без_него") is None or p_agg is None:
                continue
            linked.append({"agent": agent, "p_agg": p_agg,
                           "p_without": c["вероятность_без_него"], "outcome": res["outcome"]})
    return linked


# ── Полный прогон абляции ────────────────────────────────────────────────────────
def run_ablation(*, funnel_logs=FUNNEL_LOGS, write=True):
    runs = load_run_counterfactuals(funnel_logs)
    live_runs = [r for r in runs if r.get("mode") == "live"]
    influence = influence_table(runs)
    linked = load_resolved_links()
    contribution = agent_brier_contribution(linked)

    summary = {
        "spec_ref": "§11.1 абляция; §10 правила устойчивой калибровки (N≥30, значимость)",
        "сгенерировано": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_прогонов_всего": len(runs),
        "n_прогонов_live": len(live_runs),
        "n_прогонов_mock_тестовых": len(runs) - len(live_runs),
        "n_разрешённых_исходов_связок": len(linked),
        "таблица_влияния_drop_one": influence,
        "вклад_в_brier": contribution,
        "вывод": (
            "контрфакты по прогонам считаются (drop-one), но разрешённых форвард-исходов нет → "
            "вклад в Brier НЕ определён (накапливается; §10 требует N≥30). Применение весов "
            "не предлагается." if not linked else
            f"абляция по {len(linked)} связкам исход↔контрфакт; предложения — см. ниже"),
        "применение": "ТОЛЬКО через /apply-weights (ежемесячно, §10). Здесь — ПРЕДЛОЖЕНИЯ.",
    }
    if write:
        _write_proposed(summary)
    return summary


def _write_proposed(summary):
    L = ["# Абляция вкладов агентов — ПРЕДЛОЖЕНИЯ (§11.1, §10)", "",
         f"_Сгенерировано {summary['сгенерировано']}; применение только через /apply-weights._", "",
         f"- Прогонов с контрфактами: **{summary['n_прогонов_всего']}** "
         f"(live: {summary['n_прогонов_live']}, mock/тестовых: {summary['n_прогонов_mock_тестовых']})",
         f"- Связок исход↔контрфакт: **{summary['n_разрешённых_исходов_связок']}** "
         f"(порог значимости §10: N≥{MIN_N})", "",
         f"> {summary['вывод']}", "",
         "## Таблица влияния (drop-one, без исходов — информативность голоса)", "",
         "| агент | участий | mean |сдвиг| | mean сдвиг |", "|---|---|---|---|"]
    for r in summary["таблица_влияния_drop_one"]:
        L.append(f"| {r['agent']} | {r['n_участий']} | {r['mean_abs_shift']} | {r['mean_shift']} |")
    L += ["", "## Вклад в Brier (форвард-исходы)", ""]
    if not summary["вклад_в_brier"]:
        L.append("- _нет разрешённых исходов — вклад не определён (накапливается, П8)_")
    else:
        L.append("| агент | N исходов | mean Brier-дельта | помогал | значимо §10 | вывод |")
        L.append("|---|---|---|---|---|---|")
        for r in summary["вклад_в_brier"]:
            L.append(f"| {r['agent']} | {r['n_исходов']} | {r['mean_brier_delta']} | "
                     f"{r['n_помогал']} | {'да' if r['значимо_§10'] else 'нет'} | {r['вывод']} |")
    PROPOSED.parent.mkdir(parents=True, exist_ok=True)
    PROPOSED.write_text("\n".join(L) + "\n", encoding="utf-8")
