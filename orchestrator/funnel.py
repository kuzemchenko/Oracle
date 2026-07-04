# -*- coding: utf-8 -*-
"""orchestrator/funnel.py — воронка §6 и сбор поля суждений Дирижёра (§5).

Этот модуль = Дирижёр-без-мнения (§5): он маршрутизирует агентов, собирает их суждения в
СТАНДАРТНОЕ поле суждений (§5.2), строит карту противоречий (§5.4), считает
КОНТРФАКТИЧЕСКИЙ протокол для абляции (§11.1) и накладывает процедурное вето (§5.6: нет
источников / нарушение П8). Он НЕ оценивает рынок сам.

Гейт Недели 5–6 (§24): «Сквозной прогон — все школы выдвигают кандидатов, поле суждений
собирается в стандартном формате». Состязательный контур (генератор↔критик↔судья),
экстремизация с поправкой на корреляцию и судейская вероятность — Неделя 7 (помечено TODO).
"""
import json
import math
import pathlib
import datetime
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"

import sys
sys.path.insert(0, str(ROOT))
from agents.registry import AGENTS, schools as school_specs  # noqa: E402
from orchestrator import agents as A                          # noqa: E402
from orchestrator import context as C                         # noqa: E402
from orchestrator import openrouter as OR                     # noqa: E402
from orchestrator import debate as DBT                        # noqa: E402
from orchestrator import synthesis as SY                      # noqa: E402
from orchestrator import run_budget as RB                      # noqa: E402
from orchestrator import forecast as FC                         # noqa: E402
from orchestrator import progress as PROG                       # noqa: E402
from mathlib import portfolio as PF                            # noqa: E402

QUARANTINE_SCHOOLS = {"b_omens"}   # §4: агент примет — в карантин, в консенсус не входит
TOP_DEBATE = 7                     # §6 этап 4: топ 5–7 в дебаты
TOP_OUTPUT = 3                     # §6 этап 6: выдача топ-3
MANIP_BLOCK_DEFAULT = 7            # порог манип-балла для стоп-фильтра, если нет в thresholds


def manip_block_threshold(thresholds):
    """F0#4: ЕДИНЫЙ порог стоп-фильтра манипуляции (§4/§14, шкала балла агента 0–10). Раньше funnel
    читал ключ `block_score`, event_first — `балл_порог`/`score_threshold` (дефолт 70!), а config даёт
    `score_block_threshold: 7.0` — оба читателя мимо ключа → антиманип-вето было НЕДОСТИЖИМО. Один ключ."""
    return float((thresholds.get("manipulation") or {}).get("score_block_threshold", MANIP_BLOCK_DEFAULT))


def _now_compact():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Поле суждений в стандартном формате §5.2 ─────────────────────────────────────
def judgment_field(records):
    """Из записей агентов собирает поле суждений §5.2: вывод + вероятность + уверенность +
    данные-основания + что неизвестно (для каждого агента, единый формат)."""
    field = []
    for r in records:
        row = {
            "agent": r["agent"], "title": r["title"], "block": r["block"],
            "is_school": r["agent"] in {s[0] for s in school_specs()},
            "model": r.get("model"), "model_role": r["model_role"],
            "output_kind": r["output_kind"], "ok": r.get("ok", False),
        }
        if r.get("ok"):
            j = r["judgment"]
            row.update({
                "вывод": j.get("вывод"),
                "вероятность": j.get("вероятность"),
                "уверенность": j.get("уверенность"),
                "данные_основания": j.get("данные_основания", []),
                "что_неизвестно": j.get("что_неизвестно", []),
                "p8_clean": r.get("p8_clean"),
                "p8_violations": r.get("p8_violations", []),
                "no_data": r.get("no_data", False),
            })
            # видоспецифичные хвосты — тоже в поле, но не ломают единый каркас
            for extra in ("кандидаты", "вердикт", "балл", "переворот", "отыграно_pct"):
                if extra in j:
                    row[extra] = j[extra]
        else:
            row.update({"вывод": None, "ошибка": r.get("error"), "stage": r.get("stage")})
        field.append(row)
    return field


# ── Кандидаты школ (§6 этап 2) ───────────────────────────────────────────────────
def collect_candidates(records):
    school_ids = {s[0] for s in school_specs()}
    cands = []
    for r in records:
        if not r.get("ok") or r["agent"] not in school_ids:
            continue
        j = r["judgment"]
        for c in j.get("кандидаты", []) or []:
            cands.append({
                "школа": r["agent"], "актив": c.get("актив"),
                "направление": str(c.get("направление", "")).strip().lower(),
                "тезис": c.get("тезис"), "горизонт": c.get("горизонт"),
                "разрешимость": c.get("разрешимость"),
                "вероятность_школы": j.get("вероятность"),
            })
    return cands


# ── Карта противоречий (§5.4) ────────────────────────────────────────────────────
def contradiction_map(candidates):
    """Расхождения НЕ усредняются молча: один актив с противоположными направлениями →
    эскалация в дебаты по точке расхождения; неразрешённое противоречие понижает уверенность."""
    by_asset = defaultdict(lambda: defaultdict(list))
    for c in candidates:
        if c["актив"]:
            by_asset[c["актив"]][c["направление"]].append(c["школа"])
    contradictions = []
    for asset, dirs in by_asset.items():
        if "лонг" in dirs and "шорт" in dirs:
            contradictions.append({
                "актив": asset,
                "лонг": dirs["лонг"], "шорт": dirs["шорт"],
                "эскалация": "дебаты по точке расхождения (§5.4)",
            })
    return contradictions


# ── Контрфактический протокол абляции (§11.1) ────────────────────────────────────
def counterfactual_protocol(records):
    """Дирижёр на синтезе сохраняет, какой была бы агрегированная вероятность БЕЗ голоса
    каждого агента (drop-one пересчёт — дёшево, это математика, не повторные LLM-вызовы).
    Сырьё для ежемесячной абляции (§11.1): улучшал голос Brier итога или ухудшал."""
    voters = [(r["agent"], r["judgment"]["вероятность"])
              for r in records
              if r.get("ok") and r.get("judgment", {}).get("вероятность") is not None
              and r.get("p8_clean")]
    probs = [p for _, p in voters]
    n = len(probs)
    aggregate = round(sum(probs) / n, 4) if n else None
    counterfactuals = []
    for agent, p in voters:
        rest = [q for a, q in voters if a != agent]
        cf = round(sum(rest) / len(rest), 4) if rest else None
        counterfactuals.append({
            "без_агента": agent,
            "вероятность_без_него": cf,
            "сдвиг": round(cf - aggregate, 4) if (cf is not None and aggregate is not None) else None,
        })
    return {
        "метод": "drop-one среднее чистых (p8) голосов с вероятностью; экстремизация §5.5 — Неделя 7",
        "n_голосов": n,
        "агрегированная_вероятность": aggregate,
        "контрфакты": counterfactuals,
    }


# ── Процедурное вето (§5.6) ──────────────────────────────────────────────────────
def procedural_veto(records):
    flags = []
    for r in records:
        if not r.get("ok"):
            flags.append({"agent": r["agent"], "причина": "агент не дал валидного суждения",
                          "деталь": r.get("error"), "stage": r.get("stage")})
        elif not r.get("p8_clean"):
            flags.append({"agent": r["agent"], "причина": "нарушение П8 (источники/основания)",
                          "деталь": r.get("p8_violations")})
    return flags


# ── Этап 1. Широкий скан + FDR-контроль (§6, §23.1) ──────────────────────────────
def stage1_scan(ctx):
    """Перечисляет сырые сигналы (новости + аномалии индикаторов) и применяет FDR (БХ, §6).

    Аномалия = сигнал ТОЛЬКО при q-value < q_value_max (config/thresholds). Без FDR при многих
    проверках система «найдёт» закономерности в шуме гарантированно. P-значение аномалии —
    двусторонний нормальный survival по z-скору хода/объёма (документированная аппроксимация).
    П8: полноценный скан 200–500 сигналов требует более широкого фида — здесь сканируем то, что
    подключено (универсум + ≈1 мес. новостей); ограничение помечается честно.
    """
    import math
    from mathlib import fdr
    q_max = ((ctx.get("calibration_status") or {}).get("fdr") or {}).get("q_value_max") or 0.1
    raw_signals, pvals, labels = [], [], []
    for sym, ind in (ctx.get("indicators") or {}).items():
        for metric in ("ret_z_20", "vol_z_20"):
            z = ind.get(metric)
            if isinstance(z, (int, float)):
                p = math.erfc(abs(z) / math.sqrt(2))  # двусторонний normal survival
                pvals.append(max(min(p, 1.0), 0.0))
                labels.append({"символ": sym, "метрика": metric, "z": round(z, 3)})
    n_news = len(ctx.get("news") or [])
    bh = fdr.benjamini_hochberg(pvals, q=q_max) if pvals else {"rejected": [], "qvalues": [], "n_signif": 0}
    for i, lab in enumerate(labels):
        sig = {**lab, "p_value": round(pvals[i], 4),
               "q_value": round(bh["qvalues"][i], 4) if bh.get("qvalues") else None,
               "сигнал_после_FDR": bool(bh["rejected"][i]) if bh.get("rejected") else False}
        raw_signals.append(sig)
    return {
        "сырых_проверок": len(pvals), "новостных_заголовков": n_news,
        "q_value_max": q_max, "процедура": "benjamini_hochberg",
        "сигналов_после_FDR": int(bh.get("n_signif", 0)),
        "сигналы": raw_signals,
        "ограничение_П8": ("полный широкий скан 200–500 сигналов требует более широкого фида; "
                           "сканируем подключённое (универсум + ≈1 мес. новостей)"),
    }


def _candidate_slice_for(agent_id, candidate, ctx, thresholds):
    """User-промпт для пер-кандидатной проверки на этапе 3 (неочевидность/тайминг/манипуляция)."""
    asset = candidate.get("актив")
    payload = {
        "идея": {"актив": asset, "направление": candidate.get("направление"),
                 "тезис": candidate.get("тезис"), "разрешимость": candidate.get("разрешимость")},
        "котировка": (ctx or {}).get("quotes", {}).get(asset, {}).get("last"),
        "индикаторы": (ctx or {}).get("indicators", {}).get(asset),
        "опционы": (ctx or {}).get("options", {}).get(asset),   # IV/skew/OI по активу идеи (где есть)
        "news": (ctx or {}).get("news", [])[:6],
    }
    if agent_id == "d_timeliness":
        payload["timing_thresholds"] = (thresholds.get("timing") or {})
    elif agent_id == "c_non_obviousness":
        payload["non_obviousness_thresholds"] = (thresholds.get("non_obviousness") or {})
        payload["timing_thresholds"] = (thresholds.get("timing") or {})
    else:
        payload["manipulation_thresholds"] = (thresholds.get("manipulation") or {})
    return ("Пер-кандидатная проверка этапа 3 воронки (§6). Только поданные данные (П8).\n\n"
            "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1, default=str) +
            "\n```\n\nВерни РОВНО один объект JSON по контракту.")


# ── Этап 3. Грубая фильтрация → ~15 (§6) ─────────────────────────────────────────
def stage3_coarse_filter(candidates, records_by_id, thresholds, ctx, client):
    """Стоп-фильтры §6: карантин, дедуп (структурно), затем ПЕР-КАНДИДАТНО тайминг и манипуляция.

    Тайминг ПОЗДНО/ЛОВУШКА без контр-сценария и манип-балл ≥ порога — стоп-фильтры §6,
    они касаются КОНКРЕТНОЙ идеи, поэтому d_timeliness и d_anti_manipulation вызываются на
    срезе кандидата (поле суждений §5.2 дало лишь дневной тематический read). Run-level
    вердикты неочевидности/компетенции применяются как тематические. Каждый отсев журналируется
    с причиной (прозрачность §6 этап 6). Возвращает (выжившие, отсев, пер-кандидатные вердикты).
    """
    dropped = []
    manip_thr = manip_block_threshold(thresholds)
    ctx_filter = records_by_id.get("c_context_filter")
    out_of_competence = bool(ctx_filter and ctx_filter.get("ok") and str(ctx_filter["judgment"].get("вердикт")).upper() == "ШТРАФ")

    # 1) карантин (агент примет) — в консенсус не входит
    survivors = []
    for c in candidates:
        if c.get("школа") in QUARANTINE_SCHOOLS:
            dropped.append({**c, "причина_отсева": "карантинная школа (агент примет, §4) — журналируется, не в консенсус"})
        else:
            survivors.append(c)

    # 2) дедуп по (актив, направление): одинаковые идеи разных школ = одна (макс. P школы)
    best = {}
    for c in survivors:
        key = (c.get("актив"), c.get("направление"))
        cur = best.get(key)
        if cur is None or (c.get("вероятность_школы") or 0) > (cur.get("вероятность_школы") or 0):
            if cur is not None:
                dropped.append({**cur, "причина_отсева": f"дубликат идеи {key} (оставлена версия с большей P школы)"})
            merged = (cur.get("_школы_дубликаты", []) if cur else [])
            if cur:
                merged = merged + [cur["школа"]]
            best[key] = {**c, "_школы_дубликаты": merged}
        else:
            dropped.append({**c, "причина_отсева": f"дубликат идеи {key}"})
    deduped = list(best.values())

    # 3) run-level фильтр компетенции (§13). Неочевидность — пер-идейно ниже (§7/П5):
    #    публичность 1-го порядка ≠ отыгранность дальнего звена, нельзя стирать набор заголовком.
    after_theme = []
    for c in deduped:
        if out_of_competence:
            dropped.append({**c, "причина_отсева": "фильтр контекста: вне круга компетенции при среднем потенциале (§6/§13)"})
        else:
            after_theme.append(c)

    # 4) ПЕР-КАНДИДАТНО: неочевидность (отыгранность В ЦЕНЕ §7/П5), тайминг и манипуляция (стоп-фильтры §6)
    kept, per_cand = [], []
    for c in after_theme:
        nz = A.call_agent("c_non_obviousness", ctx, client,
                          user_prompt=_candidate_slice_for("c_non_obviousness", c, ctx, thresholds))
        tm = A.call_agent("d_timeliness", ctx, client,
                          user_prompt=_candidate_slice_for("d_timeliness", c, ctx, thresholds))
        mp = A.call_agent("d_anti_manipulation", ctx, client,
                          user_prompt=_candidate_slice_for("d_anti_manipulation", c, ctx, thresholds))
        nv = str((nz["judgment"].get("вердикт") if nz.get("ok") else "")).upper()
        tv = str((tm["judgment"].get("вердикт") if tm.get("ok") else "")).upper()
        mscore = mp["judgment"].get("балл") if mp.get("ok") else None
        per_cand.append({"актив": c.get("актив"), "направление": c.get("направление"),
                         "неочевидность": nv, "тайминг": tv, "манип_балл": mscore})
        reasons = []
        if nv == "ШТРАФ":
            reasons.append("неочевидность: идея уже отражена в цене (§7/П5)")
        if tv in ("ПОЗДНО", "ЛОВУШКА"):
            reasons.append(f"тайминг {tv} без контр-сценария (§6)")
        if isinstance(mscore, (int, float)) and mscore >= manip_thr:
            reasons.append(f"манипуляционный балл {mscore} ≥ порога {manip_thr} (§6)")
        if reasons:
            dropped.append({**c, "причина_отсева": "; ".join(reasons)})
        else:
            kept.append({**c, "_неочевидность": nv, "_тайминг": tv, "_манип_балл": mscore})
    return kept, dropped, per_cand


# ── Этап 4. Полный скоринг → топ 5–7 (§6, §7) ────────────────────────────────────
def stage4_scoring(survivors, records_by_id, ctx, costs, *, top_n=TOP_DEBATE):
    scored = []
    for c in survivors:
        sc = SY.score_candidate(c, records_by_id, ctx, costs)
        scored.append({**c, "скоринг": sc, "балл": sc["total"]})
    scored.sort(key=lambda x: x["балл"], reverse=True)
    return scored[:top_n], scored


# ── Этап 5. Дебаты (состязательный контур §4 блок E) → вероятность судьи ──────────
def stage5_debates(top, ctx, client, run_id, costs):
    survivors, debates, dropped = [], [], []
    for c in top:
        deb = DBT.run_debate(c, ctx, client, run_id=run_id, costs=costs)
        debates.append(deb)
        v = deb["вердикт"]
        if v["исход"] == "УСТОЯЛА":
            survivors.append({**c, "вероятность_судьи": v.get("вероятность_судьи"),
                              "base_rate": deb.get("base_rate"), "_debate": deb})
        else:
            dropped.append({"актив": c.get("актив"), "направление": c.get("направление"),
                            "причина_отсева": f"дебаты: {v['исход']} — {v.get('причина') or 'средний балл рубрики ниже порога'}",
                            "средний_балл_рубрики": v.get("средний_балл_рубрики")})
    return survivors, debates, dropped


# ── Этап 6. Риск + портфель + синтез отчёта → выдача топ-3 (§6, §7, §8) ───────────
def stage6_synthesis(debate_survivors, records_by_id, ctx, client, costs, limits,
                     *, run_id="funnel", seal_predictions=False, predictions_path=None,
                     now_dt=None):
    rescored = []
    for c in debate_survivors:
        sc = SY.score_candidate(c, records_by_id, ctx, costs, judge_prob=c.get("вероятность_судьи"))
        rescored.append({**c, "скоринг": sc, "балл": sc["total"]})
    rescored.sort(key=lambda x: x["балл"], reverse=True)

    # диверсификация топ-3 по макро-драйверам (§6: не три версии одной ставки)
    chosen, seen_drivers, deferred = [], set(), []
    for c in rescored:
        drv = PF.macro_driver(c.get("актив"))
        if len(chosen) < TOP_OUTPUT and drv not in seen_drivers:
            chosen.append(c); seen_drivers.add(drv)
        else:
            deferred.append(c)
    # добор, если не хватило разнообразия драйверов
    for c in deferred:
        if len(chosen) < TOP_OUTPUT:
            chosen.append(c)

    # риск-агент по каждой идее топ-3 (его «нет» перебивает поле, §5)
    gate_passed = bool((limits.get("_gate_passed_override")))  # по умолчанию False (этап Д §11)
    for c in chosen:
        c["риск"] = SY.run_risk(c, ctx, client, costs)

    # портфель (детерминированный код): размеры + карта корреляций + лимит-ворота
    portfolio_ideas = [{"актив": c["актив"], "направление": c.get("направление"),
                        "вероятность": c.get("вероятность_судьи"), "b": 1.0} for c in chosen]
    portfolio = PF.build_portfolio(portfolio_ideas, capital=limits.get("capital_anchor_usd", 100000),
                                   gate_passed=gate_passed, limits=limits)

    # ── Запечатывание §9 ДО синтеза отчёта (инвариант 3, скилл run-funnel п.6) ───────
    # Прогноз каждой выдаваемой идеи запечатывается mathlib.seal ПРЕЖДЕ, чем собран и показан
    # отчёт. Запечатываем ТОЛЬКО на боевом прогоне (seal_predictions=True) — mock/masked журнал
    # форвард-прогнозов НЕ трогают (П16). Неразрешимую идею честно не запечатываем (П8).
    for c in chosen:
        pred, reason = FC.build_forward_prediction(
            c, ctx, run_id=run_id, kind="funnel_forward", now_dt=now_dt,
            probability=c.get("вероятность_судьи"))
        if pred is None:
            c["_seal"] = {"sealed": False, "причина": reason}
            continue
        if seal_predictions:
            sealed = FC.seal_prediction(pred, path=predictions_path)
            if sealed is None:
                # ревью 2026-07-04: идентичная ставка уже в журнале (перезапуск того же дня) —
                # дубль честно НЕ запечатан, идея из выдачи не выпадает
                c["_seal"] = {"sealed": False,
                              "причина": "дубль: идентичная ставка уже запечатана (идемпотентность)"}
                continue
            c["_seal"] = {"sealed": True, "hash": sealed["hash"], "sealed_at": sealed["sealed_at"],
                          "asset": sealed["asset"], "direction": sealed["direction"],
                          "threshold": sealed["threshold"], "resolve_by": sealed["resolve_by"],
                          "price_source": sealed["price_source"], "probability": sealed["probability"]}
        else:
            c["_seal"] = {"sealed": False, "причина": "не боевой прогон (mock/masked) — не запечатываем",
                          "прогноз_§9_preview": pred}

    # синтез отчёта §8 по каждой идее
    reports = []
    for c in chosen:
        pos = next((p for p in portfolio["позиции"] if p["актив"] == c["актив"]), None)
        bundle = {
            "идея": {k: c.get(k) for k in ("актив", "направление", "тезис", "разрешимость", "школа")},
            "вероятность_судьи": c.get("вероятность_судьи"), "base_rate": c.get("base_rate"),
            "скоринг": c["скоринг"], "риск": SY._rec({"f_risk": c["риск"]}, "f_risk") or {"_ошибка": c["риск"].get("error")},
            "позиция_портфеля": pos,
            "вердикт_судьи": c["_debate"]["вердикт"],
            "позиции_критика_и_судьи": {
                "критик": SY._rec({"x": c["_debate"]["реплики"]["критик"]}, "x"),
                "судья": SY._rec({"x": c["_debate"]["реплики"]["судья"]}, "x"),
            },
        }
        rep = SY.synthesize_report(bundle, ctx, client)
        reports.append({"актив": c["актив"], "направление": c.get("направление"),
                        "балл": c["балл"], "отчёт": rep, "позиция": pos,
                        "запечатанный_прогноз_§9": c.get("_seal")})
    return {"топ_идеи": chosen, "переоценка": rescored, "портфель": portfolio, "отчёты": reports,
            "gate_калибровки_пройден": gate_passed}


# ── Прогон воронки ───────────────────────────────────────────────────────────────
class _GuardChain:
    """Цепочка бюджет-гардов (кросс-ревью ночи 04.07): внутренняя воронка event_first платит и в
    СВОЙ per-run потолок, и во ВНЕШНИЙ гард контура — раньше траты внутренних funnel были
    невидимы стопу-на-лету event_first (реальный потолок ≈ k×cap(funnel)+cap(event_first))."""
    def __init__(self, *guards):
        self.guards = [g for g in guards if g is not None]

    def add(self, cost_usd):
        for g in self.guards:
            g.add(cost_usd)

    def __call__(self, cost_usd):
        self.add(cost_usd)


def run_funnel(theme="brent", mode="auto", agent_ids=None, run_id=None, write=True, full=True,
               theme_focused=False, cost_guard=None):
    """Обёртка graceful-стопа бюджета (§24, долг[HIGH] из stage-review F0): RunBudgetGuard рвёт
    прогон на лету через RunBudgetExceeded(BaseException) — ловим ЯВНО здесь и отдаём протокол-стоп
    (не крэш). Вся логика — в _run_funnel."""
    try:
        return _run_funnel(theme=theme, mode=mode, agent_ids=agent_ids, run_id=run_id,
                           write=write, full=full, theme_focused=theme_focused,
                           cost_guard=cost_guard)
    except RB.RunBudgetExceeded as e:
        rid = run_id or f"funnel_{_now_compact()}"
        if PROG.active():
            PROG.finish(f"остановлен по бюджету (§24): ${e.spent_usd:.2f} ≥ ${e.cap_usd}")
        stop = {"run_id": rid, "ts": _now_iso(), "mode": mode, "theme": theme,
                "ОСТАНОВ_бюджет": {"mode": e.mode, "spent_usd": round(e.spent_usd, 4),
                                   "cap_usd": e.cap_usd, "reason": str(e)},
                "spec_ref": "§24 стоп-на-лету RunBudgetGuard; Инв#5 CLAUDE.md",
                "следующий_шаг": ("прогон ОСТАНОВЛЕН на лету: реальная стоимость превысила потолок "
                                  "режима; поднять может только пользователь (config/limits.yaml, П12).")}
        if write:
            _write_refusal(stop)
        return stop


def _run_funnel(theme="brent", mode="auto", agent_ids=None, run_id=None, write=True, full=True,
                theme_focused=False, cost_guard=None):
    """Сквозной прогон полной воронки §6 (этапы 1–6).

    full=True (по умолчанию): этапы 1–6 — скан+FDR → кандидаты → грубый фильтр → скоринг §7 →
        дебаты (состязательный контур §4 блок E) → риск/портфель/синтез отчёта §8 → топ-3.
    full=False: только поле суждений §5.2 (этапы 1–2) — дешёвый режим гейта Нед.5–6.
    mode: 'live' (OpenRouter) | 'mock' (без сети/трат) | 'auto'.
    Возвращает dict-протокол; при write=True пишет journal/funnel_logs/{run_id}.{json,md}.
    """
    run_id = run_id or f"funnel_{_now_compact()}"
    ctx = C.build_context(theme=theme, theme_focused=theme_focused)
    client = OR.make_client(mode=mode, run_id=run_id)
    thresholds = C._load_yaml("config/thresholds.yaml")
    limits = PF.lim.load_limits()
    costs = SY.load_costs()

    # ── ГАРД ТЕМЫ (§6/§8/П8) ПЕРЕД любым LLM-вызовом ────────────────────────────────
    # Тематический фокус разрешён ТОЛЬКО по активу из калиброванного универсума с историей
    # ≥ MIN_THEME_HISTORY_BARS. Вне универсума ИЛИ слишком короткая история → честный ранний
    # отказ «нет данных по теме» (0 трат). Иначе агенты дрейфуют к фоновым новостям и выдают
    # суждение не про тему (урок SPCX.US 2026-06-13: однодневный IPO вне ядра → разбор макро).
    structural = bool((ctx.get("theme_meta") or {}).get("structural"))
    if theme_focused:
        sym, _kind = C.resolve_theme(theme)
        nbars = ctx["quotes"].get(sym, {}).get("n_bars", 0) if sym else 0
        low_history = nbars < C.MIN_THEME_HISTORY_BARS
        # ОТКАЗ только если тема ВНЕ универсума, либо мало истории И тема НЕ структурная.
        # Структурная тема (событие-IPO) с малой историей — НЕ отказ: идём в research-режиме,
        # заякорив агентов на каскад (theme_anchor), торгуемый выход — на калибруемые звенья.
        if sym is None or (low_history and not structural):
            why = (f"тема '{theme}' вне калиброванного универсума (core_tradeable={C.CORE})"
                   if sym is None else
                   f"по '{theme}'→{sym} только {nbars} баров истории (< {C.MIN_THEME_HISTORY_BARS}): "
                   f"волатильность/индикаторы/калибровка §23 не определены, а тема не структурная")
            refusal = {
                "run_id": run_id, "ts": _now_iso(), "mode": client.mode, "theme": theme,
                "spec_ref": "§6/§8/П8 гард темы: тематический фокус — только актив универсума с историей",
                "ОТКАЗ_тема": {"resolvable": sym is not None, "matched_symbol": sym,
                               "n_bars": nbars, "min_bars": C.MIN_THEME_HISTORY_BARS, "reason": why},
                "следующий_шаг": ("прогон НЕ выполнен (0 трат): по теме нет калиброванных данных — "
                                  "агенты дрейфовали бы к фоновым новостям (урок SPCX.US). Оцени актив "
                                  "через deep-research, либо добавь в универсум после калибровки §23."),
            }
            if write:
                _write_refusal(refusal)
            return refusal

    # ── ПРЕД-проверка бюджета прогона ПЕРЕД любым LLM-вызовом (§24, долг Нед.8) ──────
    # Только для live: оценка прогона vs потолок режима и месячный потолок. Отказ → прогон
    # не начинается, возвращается протокол-отказ (ни одного платного вызова). Mock — без проверки.
    budget_mode = "funnel_full" if full else "theme_daily"
    budget_decision = None
    if getattr(client, "mode", None) == "live":
        budget_decision = RB.precheck(budget_mode, limits=limits)
        if not budget_decision["allowed"]:
            refusal = {
                "run_id": run_id, "ts": _now_iso(), "mode": client.mode, "theme": theme,
                "spec_ref": "§24 пред-проверка per_run_token_budget (долг Нед.8); инвариант 5 CLAUDE.md",
                "ОТКАЗ_бюджет": budget_decision,
                "следующий_шаг": ("прогон НЕ выполнен: оценка стоимости превышает потолок (§24) или "
                                  "месячный бюджет исчерпан (§30 п.2). Поднять потолок может только "
                                  "пользователь правкой config/limits.yaml (П12)."),
            }
            if write:
                _write_refusal(refusal)
            return refusal
        # стоп на лету (второй эшелон): рвём прогон при пересечении потолка режима.
        # Внешний cost_guard (event_first) — в цепочке: внутренние траты видны обоим потолкам.
        own = RB.RunBudgetGuard(budget_mode, budget_decision["cap_usd"])
        client.cost_guard = _GuardChain(own, cost_guard) if cost_guard is not None else own

    ids = agent_ids or [a[0] for a in AGENTS]
    # Прогресс (§15): event-first уже мог открыть прогон и выставить текущее событие через
    # PROG.outer(); тогда воронка только рапортует этапы. Иначе (тематический/одиночный
    # прогон) — открываем свой прогон на одно событие и сами его закроем.
    _owns_progress = not PROG.active()
    if _owns_progress:
        PROG.begin(run_id, getattr(client, "mode", mode), f"воронка · {theme}", outer_total=1)
        PROG.outer(0, theme)
    records = []
    for _i, aid in enumerate(ids):
        records.append(A.call_agent(aid, ctx, client))
        PROG.phase("field", agents=(_i + 1, len(ids)), detail=aid)
    records_by_id = {r["agent"]: r for r in records}

    PROG.phase("candidates")
    field = judgment_field(records)
    candidates = collect_candidates(records)
    contradictions = contradiction_map(candidates)
    counterfactual = counterfactual_protocol(records)
    veto = procedural_veto(records)

    school_ids = {s[0] for s in school_specs()}
    schools_ran = [r for r in records if r["agent"] in school_ids]
    schools_ok = [r for r in schools_ran if r.get("ok")]
    schools_with_cands = {c["школа"] for c in candidates}

    # ── Этап 1: широкий скан + FDR ───────────────────────────────────────────────
    PROG.phase("scan")
    scan = stage1_scan(ctx)

    protocol = {
        "run_id": run_id,
        "ts": _now_iso(),
        "mode": client.mode,
        "theme": theme,
        "spec_ref": "§5 Дирижёр, §6 воронка (этапы 1–6), §7 скоринг, §8 отчёт, §11.1 абляция; §24 пред-бюджет",
        "пред_проверка_бюджета": budget_decision,
        "asof_data": {s: ctx["quotes"].get(s, {}).get("last_date") for s in ctx["quotes"]},
        "data_gaps": ctx["data_gaps"],
        "agents_total": len(records),
        "agents_ok": sum(1 for r in records if r.get("ok")),
        "schools_total": len(schools_ran),
        "schools_ok": len(schools_ok),
        "schools_with_candidates": sorted(schools_with_cands),
        "candidates_count": len(candidates),
        "поле_суждений": field,
        "кандидаты": candidates,
        "карта_противоречий": contradictions,
        "контрфактический_протокол": counterfactual,
        "процедурное_вето": veto,
        "уверенность_итога": ("понижена: есть неразрешённые противоречия" if contradictions
                              else "без эскалаций противоречий на этом прогоне"),
        "этап1_скан_FDR": scan,
    }

    if not full:
        protocol["следующий_шаг"] = "full=False: этапы 3–6 не выполнялись (дешёвый режим поля суждений)"
        if write:
            _write_protocol(protocol)
        if _owns_progress:
            PROG.finish(f"воронка · {theme}: поле суждений ({len(candidates)} кандидатов), этапы 3–6 пропущены")
        return protocol

    # ── Этапы 3–6 ────────────────────────────────────────────────────────────────
    PROG.phase("coarse")
    kept3, dropped3, per_cand3 = stage3_coarse_filter(candidates, records_by_id, thresholds, ctx, client)
    PROG.phase("scoring")
    top4, scored4 = stage4_scoring(kept3, records_by_id, ctx, costs)
    PROG.phase("debates")
    deb_survivors, debates, dropped5 = stage5_debates(top4, ctx, client, run_id, costs)
    PROG.phase("synthesis")
    if deb_survivors:
        synth = stage6_synthesis(deb_survivors, records_by_id, ctx, client, costs, limits,
                                 run_id=run_id, seal_predictions=(client.mode == "live"))
    else:
        synth = {"топ_идеи": [], "переоценка": [], "портфель": None, "отчёты": [],
                 "gate_калибровки_пройден": False}

    # Воронка прозрачности §6: сколько и почему отсеяно на каждом этапе
    funnel_report = {
        "этап1_сырых_сигналов": scan["сырых_проверок"],
        "этап1_сигналов_после_FDR": scan["сигналов_после_FDR"],
        "этап2_кандидатов": len(candidates),
        "этап3_после_грубого_фильтра": len(kept3),
        "этап3_отсеяно": len(dropped3),
        "этап4_в_дебаты_топ": len(top4),
        "этап5_устояло_после_дебатов": len(deb_survivors),
        "этап5_разбито_или_вето": len(dropped5),
        "этап6_выдано_топ": len(synth["отчёты"]),
        "отсев_этап3": dropped3,
        "отсев_этап5": dropped5,
        "вывод": ("стоящих идей нет — легитимный результат слабого дня (§6)"
                  if not synth["отчёты"] else f"выдано идей: {len(synth['отчёты'])}"),
    }

    protocol.update({
        "этап3_грубый_фильтр": {"выжившие": kept3, "отсеяно": dropped3, "пер_кандидатные_вердикты": per_cand3},
        "этап4_скоринг": {"топ_в_дебаты": top4, "все_оценённые": scored4},
        "этап5_дебаты": debates,
        "этап6_синтез": synth,
        "воронка_отсева": funnel_report,
        "следующий_шаг": "Нед.8 закрыта: пред-проверка per_run_token_budget (§24); маскированные "
                         "кейсы §23.2б — orchestrator/masked.py (gate ≥70%); абляция §11.1 — "
                         "orchestrator/ablation.py; дашборд §15 — dashboard/build_dashboard.py. "
                         "Открытый долг: экстремизация §5.5 (агрегат пока простое среднее)",
    })
    if write:
        _write_protocol(protocol)
    if _owns_progress:
        PROG.finish(f"воронка · {theme}: {funnel_report['вывод']}")
    return protocol


def _write_refusal(p):
    """Протокол ОТКАЗА/ОСТАНОВА (§24 бюджет ДО или НА ЛЕТУ, §6 гард темы): след в журнале."""
    FUNNEL_LOGS.mkdir(parents=True, exist_ok=True)
    jpath = FUNNEL_LOGS / f"{p['run_id']}.json"
    PROG.atomic_write_text(jpath, json.dumps(p, ensure_ascii=False, indent=2))   # M13: без битых JSON
    head = (f"- Время: {p['ts']} · режим: {p['mode']} · тема: {p['theme']}\n"
            f"- Спецификация: {p['spec_ref']}\n\n")
    if "ОТКАЗ_бюджет" in p:
        d = p["ОТКАЗ_бюджет"]
        md = (f"# ОТКАЗ прогона по бюджету · {p['run_id']}\n" + head +
              f"**{d['reason']}**\n\n"
              f"- Контур отказа: {d.get('контур')}\n"
              f"- Оценка прогона: ${d['estimate_usd']} = {d['expected_calls']} вызовов × "
              f"${d['avg_call_usd']} ({d['basis_avg']}, n_истории={d['n_history']})\n"
              f"- Месячный спенд: ${d['spent_month_usd']} / потолок ${d['month_cap_usd']}\n\n"
              f"> {p['следующий_шаг']}\n")
    elif "ОСТАНОВ_бюджет" in p:
        # M2 (ревью 04.07): стоп-на-лету раньше журналировался как «тема вне универсума» с
        # reason=None — ложная запись на money-пути §24. Теперь честный md.
        d = p["ОСТАНОВ_бюджет"]
        md = (f"# ОСТАНОВ прогона по бюджету НА ЛЕТУ · {p['run_id']}\n" + head +
              f"**{d.get('reason')}**\n\n"
              f"- Потрачено к моменту стопа: ${d.get('spent_usd')} ≥ потолка ${d.get('cap_usd')} "
              f"(контур {d.get('mode')})\n\n"
              f"> {p.get('следующий_шаг')}\n")
    else:
        d = p.get("ОТКАЗ_тема", {})
        md = (f"# ОТКАЗ прогона: тема вне универсума · {p['run_id']}\n" + head +
              f"**{d.get('reason')}**\n\n"
              f"- Тема сводится к: {d.get('matched_symbol')} · баров истории: {d.get('n_bars')} "
              f"(нужно ≥ {d.get('min_bars')})\n\n"
              f"> {p['следующий_шаг']}\n")
    (FUNNEL_LOGS / f"{p['run_id']}.md").write_text(md, encoding="utf-8")
    return jpath


def _write_protocol(p):
    FUNNEL_LOGS.mkdir(parents=True, exist_ok=True)
    jpath = FUNNEL_LOGS / f"{p['run_id']}.json"
    PROG.atomic_write_text(jpath, json.dumps(p, ensure_ascii=False, indent=2))   # M13
    mpath = FUNNEL_LOGS / f"{p['run_id']}.md"
    PROG.atomic_write_text(mpath, _render_md(p))
    return jpath, mpath


def _render_md(p):
    L = []
    L.append(f"# Протокол прогона воронки · {p['run_id']}")
    L.append(f"- Время: {p['ts']} · режим: **{p['mode']}** · тема: {p['theme']}")
    L.append(f"- Спецификация: {p['spec_ref']}")
    L.append(f"- Агентов: {p['agents_ok']}/{p['agents_total']} ок · "
             f"школ: {p['schools_ok']}/{p['schools_total']} · "
             f"кандидатов: {p['candidates_count']}")
    bd = p.get("пред_проверка_бюджета")
    if bd:
        L.append(f"- Пред-проверка бюджета (§24): оценка **${bd['estimate_usd']}** ≤ потолка ${bd.get('cap_usd')} "
                 f"режима '{bd['mode']}' · {bd['expected_calls']}×${bd['avg_call_usd']} ({bd['basis_avg']}) · "
                 f"месяц ${bd['spent_month_usd']}/${bd['month_cap_usd']}")
    L.append("")
    L.append("## Поле суждений (стандартный формат §5.2)")
    L.append("| Агент | Блок | Школа | Модель | Вывод | P | Увер. | П8 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in p["поле_суждений"]:
        prob = "—" if r.get("вероятность") is None else f"{r['вероятность']:.2f}"
        p8 = "—"
        if r.get("ok"):
            p8 = "✓" if r.get("p8_clean") else "✗"
        vyvod = (r.get("вывод") or r.get("ошибка") or "—")
        vyvod = str(vyvod)[:48].replace("|", "/")
        L.append(f"| {r['agent']} | {r['block']} | {'да' if r['is_school'] else '—'} | "
                 f"{r.get('model','—')} | {vyvod} | {prob} | {r.get('уверенность','—')} | {p8} |")
    L.append("")
    cf = p["контрфактический_протокол"]
    L.append("## Контрфактический протокол (абляция §11.1)")
    L.append(f"- Агрегированная вероятность: **{cf['агрегированная_вероятность']}** "
             f"по {cf['n_голосов']} чистым голосам ({cf['метод']})")
    if cf["контрфакты"]:
        L.append("| Без агента | P без него | Сдвиг |")
        L.append("|---|---|---|")
        for c in cf["контрфакты"]:
            L.append(f"| {c['без_агента']} | {c['вероятность_без_него']} | {c['сдвиг']} |")
    L.append("")
    L.append("## Карта противоречий (§5.4)")
    if p["карта_противоречий"]:
        for c in p["карта_противоречий"]:
            L.append(f"- **{c['актив']}**: лонг {c['лонг']} ↔ шорт {c['шорт']} → {c['эскалация']}")
    else:
        L.append("- противоречий направлений по активам не обнаружено")
    L.append("")
    L.append("## Процедурное вето (§5.6)")
    if p["процедурное_вето"]:
        for v in p["процедурное_вето"]:
            L.append(f"- {v['agent']}: {v['причина']} — {v.get('деталь')}")
    else:
        L.append("- нарушений процедуры/П8 не зафиксировано")
    L.append("")
    # ── Воронка отсева §6 (этапы 1–6): сколько и почему отсеяно ──────────────────
    fr = p.get("воронка_отсева")
    if fr:
        L.append("## Воронка отбора §6 — отсев по этапам")
        L.append("| Этап | Осталось | Отсеяно |")
        L.append("|---|---|---|")
        L.append(f"| 1. Скан+FDR | {fr['этап1_сигналов_после_FDR']} сигн. из {fr['этап1_сырых_сигналов']} проверок | "
                 f"{fr['этап1_сырых_сигналов'] - fr['этап1_сигналов_после_FDR']} (шум) |")
        L.append(f"| 2. Кандидаты | {fr['этап2_кандидатов']} | — |")
        L.append(f"| 3. Грубый фильтр | {fr['этап3_после_грубого_фильтра']} | {fr['этап3_отсеяно']} |")
        L.append(f"| 4. Скоринг §7 → топ | {fr['этап4_в_дебаты_топ']} | {fr['этап3_после_грубого_фильтра'] - fr['этап4_в_дебаты_топ']} |")
        L.append(f"| 5. Дебаты (контур E) | {fr['этап5_устояло_после_дебатов']} | {fr['этап5_разбито_или_вето']} |")
        L.append(f"| 6. Выдача топ-3 | **{fr['этап6_выдано_топ']}** | {fr['этап5_устояло_после_дебатов'] - fr['этап6_выдано_топ']} (диверсификация) |")
        L.append("")
        if fr.get("отсев_этап3"):
            L.append("**Отсев на этапе 3 (причины):**")
            for d in fr["отсев_этап3"]:
                L.append(f"- {d.get('актив')} {d.get('направление','')}: {d.get('причина_отсева')}")
        if fr.get("отсев_этап5"):
            L.append("**Отсев на этапе 5 (дебаты):**")
            for d in fr["отсев_этап5"]:
                L.append(f"- {d.get('актив')} {d.get('направление','')}: {d.get('причина_отсева')}")
        L.append(f"\n> **Итог воронки:** {fr['вывод']}")
        L.append("")

    # ── Состязательный контур §4 блок E: слепота, рандомизация, рубрика ──────────
    debates = p.get("этап5_дебаты")
    if debates:
        L.append("## Состязательный контур §4 блок E (этап 5)")
        for d in debates:
            v = d["вердикт"]
            L.append(f"### {d['актив']} {d.get('направление','')} (школа {d.get('школа')}) — **{v['исход']}**")
            L.append(f"- Слепое дело: метки {d['слепое_дело']['метки_в_деле']} в случайном порядке "
                     f"(seed из run_id+актив); карта меток — только в аудит-протоколе, судье не передана")
            L.append(f"- П10: семья генератора **{d['семейство_генератора']}** ≠ семья судьи **{d['семейство_судьи']}**")
            L.append(f"- Рубрика: средний балл {v.get('средний_балл_рубрики')} vs порог {v.get('порог')} → {v['исход']}; "
                     f"судья заявил: {v.get('судья_заявил')}")
            if v.get("примечание"):
                L.append(f"- ⚠ {v['примечание']}")
            if v.get("пропущенные_вопросы"):
                L.append(f"- Процедурное вето: не отвечены вопросы {v['пропущенные_вопросы']}")
        L.append("")

    # ── Топ-идеи и портфель (этап 6) ─────────────────────────────────────────────
    synth = p.get("этап6_синтез")
    if synth and synth.get("отчёты"):
        L.append("## Выдача топ-3 + портфель (этапы 6, §7/§8)")
        port = synth.get("портфель") or {}
        L.append(f"- Режим размера: {port.get('режим_размера')}")
        L.append(f"- Суммарный риск: ${port.get('суммарный_риск_usd')} · "
                 f"независимых ставок по макро-драйверам: {(port.get('карта_корреляций') or {}).get('n_независимых_ставок')}")
        for w in (port.get("карта_корреляций") or {}).get("предупреждения", []):
            L.append(f"  - ⚠ корреляция: {w['деталь']}")
        L.append("")
        L.append("| # | Идея | Балл §7 | P судьи | Драйвер | Размер $ |")
        L.append("|---|---|---|---|---|---|")
        for i, rep in enumerate(synth["отчёты"], 1):
            pos = rep.get("позиция") or {}
            cand = next((c for c in synth["топ_идеи"] if c["актив"] == rep["актив"]), {})
            L.append(f"| {i} | {rep['актив']} {rep.get('направление','')} | {rep.get('балл')} | "
                     f"{cand.get('вероятность_судьи')} | {pos.get('макро_драйвер')} | {pos.get('amount_usd')} |")
        L.append("")

        # Полный отчёт §8 (13 обязательных полей) + запечатанный §9-прогноз по каждой идее
        for i, rep in enumerate(synth["отчёты"], 1):
            L.append(f"### Идея {i}: {rep['актив']} {rep.get('направление','')} — отчёт §8 (13 полей)")
            sd = rep.get("запечатанный_прогноз_§9") or {}
            if sd.get("sealed"):
                L.append(f"- 🔒 **Запечатан §9 ДО показа** · `{sd['hash'][:16]}…` @ {sd['sealed_at']}")
                L.append(f"  - прогноз: **{sd['asset']} {sd['direction']} {sd['threshold']}** "
                         f"(P={sd['probability']}) до {sd['resolve_by']} по {sd['price_source']}")
            else:
                L.append(f"- ⚠ НЕ запечатан (§9/П8): {sd.get('причина','—')}")
            j = (rep.get("отчёт") or {}).get("judgment") or {}
            fields = j.get("поля") or {}
            if fields:
                for k in sorted(fields, key=lambda x: int(str(x).split("_")[0]) if str(x).split("_")[0].isdigit() else 99):
                    v = fields[k]
                    if isinstance(v, list):
                        L.append(f"- **{k}:**")
                        for item in v:
                            L.append(f"  - {item}")
                    else:
                        L.append(f"- **{k}:** {v}")
            else:
                L.append("- ⚠ синтезатор не вернул 13 полей §8 (см. JSON-протокол)")
            L.append("")
    elif synth is not None:
        L.append("## Выдача")
        L.append(f"- {(p.get('воронка_отсева') or {}).get('вывод', 'стоящих идей нет (§6)')}")
        L.append("")

    L.append("## Честные пробелы данных (П8)")
    for g in p["data_gaps"]:
        L.append(f"- {g}")
    L.append("")
    L.append(f"> Следующий шаг: {p['следующий_шаг']}")
    return "\n".join(L) + "\n"
