# -*- coding: utf-8 -*-
"""orchestrator/masked.py — харнесс маскированных кейсов §23.2(б).

Легальный обходной путь запретной зоны §23.2 / инварианта 3 CLAUDE.md (П16): подавать LLM
«точку в прошлом» и оценивать её прогноз нельзя (модель помнит исход). Маскировка — имена,
даты, страны и числа нейтрализованы — проверяет не знание исхода, а КАЧЕСТВО РАССУЖДЕНИЯ:
строит ли контур правильный каскад, ловит ли ловушку/отыгранность, учитывает ли тайминг,
манипуляцию, разрешимость §9, net-асимметрию (6 критериев config/rubric.yaml).

Что проверяется — контур дебатов §4 блок E (генератор→критик→адвокат→reviewer→СЛЕПОЙ судья).
Именно эти роли видят кейс (см. masked_cases/README). Тайминг и манипуляция учтены как
критерии судейской рубрики (timing_accounted, anti_manipulation_passed).

Строгое разделение (защита от утечки ответа):
  • `_agent_payload(case)` отдаёт контуру СТРОГО `подаётся_агентам` — без исхода, направления,
    оригинала. Блок `эталон_АУДИТ` (ожидаемое направление, expected_stance, подсказка) НИКОГДА
    не покидает этот модуль в сторону агентов: он нужен только детерминированной оценке и
    человеку (§25). Тест test_masked_no_leak проверяет, что аудит не попадает в payload.

Оценка — ДЕТЕРМИНИРОВАННАЯ (mathlib.masked_eval, инвариант 6): судья выставляет баллы рубрики
(LLM), но зачёт кейса и агрегат доли ≥0.70 считает код. Смешанный набор affirm/reject ловит
вырожденные стратегии «подтверждай всё»/«отвергай всё».

Честность результата (П8): маскировка несовершенна (родовые связки опознаются как КЛАСС) —
каждый результат помечается `маскировка_несовершенна=true`, агрегат несёт оговорку
«ориентировочный, не доказательный». Результаты пишутся в reports/masked/ — НЕ в
journal/predictions.jsonl (это не запечатанные форвард-прогнозы, П16).

Бюджет (§24, инвариант 5): live-прогон проходит пред-проверку per_run_token_budget режима
masked_smoke (5 вызовов × N кейсов) ДО первого вызова; отказ при превышении.

Запуск:
    python3 orchestrator/run.py --mode masked            # live при ключе, иначе mock
    python3 orchestrator/run.py --mode masked --mock      # принудительно mock (дымовой)
"""
import json
import pathlib
import datetime

import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from orchestrator import debate as D          # noqa: E402
from orchestrator import openrouter as OR     # noqa: E402
from orchestrator import run_budget as RB     # noqa: E402
from mathlib import masked_eval as ME         # noqa: E402

CASES_DIR = ROOT / "knowledge" / "masked_cases"
REPORTS_DIR = ROOT / "reports" / "masked"
BUDGET_MODE = "masked_smoke"
MAX_PARALLEL_CASES = 6    # одновременных кейсов в live (кап запросов к OpenRouter, защита от 429)

# СТОЙКО-НЕЙТРАЛЬНАЯ пометка судье (§23.2б): задаёт ПРАВИЛЬНЫЙ стандарт оценки маскированного
# кейса. НЕ раскрывает направление/исход и НЕ просит снисхождения — ловушку не вытянет
# (манипуляция/отыгранность валятся по существу). Цель — чтобы судья не применял к намеренно
# обезличенной исторической ситуации стандарт «мало внешних источников» вместо оценки СТРУКТУРЫ.
EVAL_CONTEXT = (
    "Это МАСКИРОВАННЫЙ исторический кейс (§23.2б): имена/страны/даты/идентичности нейтрализованы "
    "НАМЕРЕННО, величины даны нейтральными синтетическими ориентирами. Оценивай КАЧЕСТВО "
    "РАССУЖДЕНИЯ по рубрике — корректность каскада, тайминг, антиманипуляцию, разрешимость, "
    "net-асимметрию — на ПОДАННЫХ данных, а не точность прогноза. Отсутствие реальных внешних "
    "идентификаторов — это конструкция теста, НЕ выдумка автора аргумента и не повод снижать "
    "source_quality, если факты опираются на поданный срез. Стандарты строгости рубрики и оба "
    "обязательных вопроса §4 сохраняются в полной силе; ловушку/отыгранность по-прежнему разбивай."
)


# ── Загрузка кейса и СТРОГОЕ выделение того, что видят агенты ─────────────────────
def load_case(path):
    """Парсит YAML кейса целиком (включая аудит — он остаётся ВНУТРИ этого модуля)."""
    with open(path, encoding="utf-8") as f:
        case = yaml.safe_load(f)
    if "подаётся_агентам" not in case:
        raise ValueError(f"{path}: нет блока 'подаётся_агентам'")
    if "эталон_АУДИТ" not in case:
        raise ValueError(f"{path}: нет блока 'эталон_АУДИТ'")
    case.setdefault("id", pathlib.Path(path).stem)
    return case


def load_cases(cases_dir=CASES_DIR):
    return [load_case(p) for p in sorted(pathlib.Path(cases_dir).glob("mc*.yaml"))]


def _agent_payload(case):
    """ЕДИНСТВЕННОЕ, что отдаётся контуру дебатов. Берёт СТРОГО `подаётся_агентам`.

    Гарантия §23.2(б): ни `эталон_АУДИТ`, ни `expected_stance`, ни ожидаемое направление,
    ни оригинал-подсказка сюда не попадают (см. test_masked_no_leak)."""
    return dict(case["подаётся_агентам"])


def _expected_stance(case):
    """affirm|reject — берётся ТОЛЬКО из аудит-блока, агентам не показывается."""
    st = case["эталон_АУДИТ"].get("expected_stance")
    if st not in ("affirm", "reject"):
        raise ValueError(f"{case.get('id')}: expected_stance ∈ {{affirm,reject}}, дано {st!r}")
    return st


# ── Сборка дела для контура (направление СКРЫТО — контур обязан вывести его сам) ──
def _compose_thesis(p):
    """Тезис для контура = маскированная ситуация + НЕЙТРАЛЬНЫЕ количественные ориентиры
    (масштаб/σ/уровни/лаги — идентичности скрыты). Без подсказки направления. Так контур
    получает материал для квантованного каскада, тайминга и net-асимметрии (§23.2б)."""
    parts = [str(p.get("ситуация", "")).strip()]
    orient = p.get("нейтральные_количественные_ориентиры")
    if orient:
        lines = "\n".join(f"  • {k}: {v}" for k, v in orient.items())
        parts.append("Нейтральные количественные ориентиры (идентичности скрыты, "
                     "направление НЕ дано — выводишь сам):\n" + lines)
    sources = p.get("источники_среза")
    if sources:
        lines = "\n".join(
            f"  • [{s.get('источник','?')}] {s.get('факт','')} "
            f"(независимость: {s.get('независимость','?')})" for s in sources)
        parts.append("Атрибутируемые источники среза (на них можно ссылаться; reviewer проверяет):\n"
                     + lines)
    masked = p.get("числа_маскированы")
    if masked:
        parts.append("Что именно скрыто/нейтрализовано: " + "; ".join(str(m) for m in masked))
    return "\n\n".join(parts)


def _build_candidate(case):
    """Кандидат для debate.run_debate из маскированного payload. КЛЮЧЕВОЕ: направление=None,
    тезис=маскированная ситуация + нейтральные количественные ориентиры (без подсказки
    направления) — контур решает long/short/нет-идеи сам. Разрешимость не подаётся: §9-формулировку
    агенты обязаны предложить."""
    p = _agent_payload(case)
    return {
        "актив": p.get("нейтральный_тикер", f"MASK_{case['id']}"),
        "направление": None,                       # СКРЫТО: ответ кейса
        "тезис": _compose_thesis(p),               # ситуация + нейтральные ориентиры
        "школа": "masked_case",
        "вероятность_школы": None,
        "разрешимость": None,                      # агенты предлагают §9 сами
    }


def _build_ctx(case):
    """Срез контекста под нейтральный тикер из подаётся_агентам (индикаторы/котировка/издержки)."""
    p = _agent_payload(case)
    tkr = p.get("нейтральный_тикер", f"MASK_{case['id']}")
    ind = dict(p.get("индикаторы") or {})
    last = ind.get("last_close")
    ctx = {
        "quotes": {tkr: {"last": last}} if last is not None else {},
        "indicators": {tkr: ind} if ind else {},
        "calibration_status": {"thresholds_calibrated": True},
        "_masked": True,
    }
    costs = {tkr: dict(p.get("издержки") or {})}
    return ctx, costs


# ── Извлечение метрик оценки из протокола дебатов ────────────────────────────────
def _p8_violations_total(protocol):
    """Сумма нарушений П8 по ВСЕЙ цепочке ролей контура (генератор..судья)."""
    total = 0
    for rec in protocol.get("реплики", {}).values():
        v = rec.get("p8_violations")
        if isinstance(v, int):
            total += v
        elif not rec.get("ok"):
            # роль не дала валидного ответа — это не «выдумка», но фиксируем как изъян отдельно
            pass
    return total


def _failed_result(case, error, threshold):
    """Результат для УПАВШЕГО кейса — ЯВНЫЙ провал (не маршрутизируем через no_trade, иначе
    ошибочный reject-кейс ложно «зачёлся» бы как отказ). Errored кейс нельзя засчитать: качество
    рассуждения не получено."""
    stance = _expected_stance(case)
    return {
        "case_id": case["id"],
        "expected_stance": stance,
        "ожидание": "кейс не отработал (инфраструктурная ошибка)",
        "verdict_outcome": "ОШИБКА",
        "rubric_mean": None, "rubric_pct": None,
        "reasoning_correct": False,
        "p8_violations": 0, "p8_clean": True,
        "mandatory_answered": False,
        "case_passed": False,
        "почему_не_зачтён": [f"кейс упал: {type(error).__name__}: {error}"],
        "маскировка_несовершенна": "несовершенство_маскировки" in case,
        "класс_актива": case.get("класс_актива"),
        "ошибка": f"{type(error).__name__}: {error}",
    }


def _masked_threshold(rubric):
    """Отдельный порог УСТОЯЛА для маскированной регрессии §23.2(б) (rubric.masked_eval.break_threshold,
    введён с согласия пользователя). Фолбек — общий break_threshold боевой рубрики."""
    me = (rubric or {}).get("masked_eval") or {}
    return float(me.get("break_threshold",
                        (rubric or {}).get("verdict", {}).get("break_threshold", ME.DEFAULT_THRESHOLD)))


def _masked_rubric(rubric, threshold):
    """Копия рубрики с порогом адъюдикации = масккейс-порог (боевая рубрика не мутируется)."""
    import copy
    r = copy.deepcopy(rubric)
    r.setdefault("verdict", {})["break_threshold"] = threshold
    return r


def evaluate_case(case, client, *, run_id, rubric=None, costs_cfg=None, models=None, threshold=None):
    """Прогон ОДНОГО кейса через контур и его детерминированная оценка masked_eval.

    threshold: масккейс-порог УСТОЯЛА (§23.2б, rubric.masked_eval). Применяется И к адъюдикации
    контура (через масккейс-рубрику), И к зачёту masked_eval — чтобы исход и оценка были когерентны."""
    rubric = rubric or D.load_rubric()
    threshold = threshold if threshold is not None else _masked_threshold(rubric)
    candidate = _build_candidate(case)
    ctx, slice_costs = _build_ctx(case)
    protocol = D.run_debate(candidate, ctx, client, run_id=f"{run_id}:{case['id']}",
                            costs=slice_costs, rubric=_masked_rubric(rubric, threshold), models=models,
                            eval_context=EVAL_CONTEXT)
    verdict = protocol.get("вердикт", {})
    judge_rec = protocol.get("реплики", {}).get("судья", {})
    gen_rec = protocol.get("реплики", {}).get("генератор", {})
    answered, missing = D._mandatory_answered(judge_rec)
    масккировка = "несовершенство_маскировки" in case
    # направление, выведенное контуром (для reject: отказ «нет» = верно не входить; §23.2б)
    proposed_dir = D._norm_direction((gen_rec.get("judgment") or {}).get("направление")) \
        if gen_rec.get("ok") else None

    result = ME.score_case(
        case["id"],
        expected_stance=_expected_stance(case),
        rubric_mean=verdict.get("средний_балл_рубрики"),
        verdict_outcome=verdict.get("исход"),
        proposed_direction=proposed_dir,
        threshold=threshold,
        p8_violations=_p8_violations_total(protocol),
        mandatory_answered=answered,
        masking_imperfect=масккировка,
        extra={
            "класс_актива": case.get("класс_актива"),
            "направление_контура": proposed_dir,
            "семейство_генератора": protocol.get("семейство_генератора"),
            "семейство_судьи": protocol.get("семейство_судьи"),
            "пропущенные_вопросы": missing,
            "судья_заявил": verdict.get("судья_заявил"),
        },
    )
    return result, protocol


# ── Полный прогон набора ─────────────────────────────────────────────────────────
def _now_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_masked(*, mode="auto", cases_dir=CASES_DIR, write=True, limits=None):
    """Обёртка graceful-стопа бюджета (§24, долг[HIGH]): RunBudgetGuard рвёт набор на лету через
    RunBudgetExceeded(BaseException) — ловим ЯВНО (иначе пролетел бы сквозь except Exception кейса
    и крэшнул пул). Логика — в _run_masked."""
    try:
        return _run_masked(mode=mode, cases_dir=cases_dir, write=write, limits=limits)
    except RB.RunBudgetExceeded as e:
        return {"run_id": f"masked_{_now_stamp()}", "mode": mode, "spec_ref": "§23.2(б), §24",
                "ОСТАНОВ_бюджет": {"mode": e.mode, "spent_usd": round(e.spent_usd, 4),
                                   "cap_usd": e.cap_usd, "reason": str(e)},
                "вывод": f"ОСТАНОВ на лету: {e}", "gate_пройден": False}


def _run_masked(*, mode="auto", cases_dir=CASES_DIR, write=True, limits=None):
    """Прогон всего регрессионного набора §23.2(б). Возвращает агрегат с gate ≥0.70.

    mode: 'auto' (live при ключе, иначе mock) | 'live' | 'mock'.
    Бюджет: live проходит пред-проверку masked_smoke (5×N вызовов) ДО первого LLM-вызова.
    """
    cases = load_cases(cases_dir)
    n = len(cases)
    if n == 0:
        raise RuntimeError("нет маскированных кейсов — gate §24 не на чем проверять (П8)")

    run_id = f"masked_{_now_stamp()}"
    client = OR.make_client(mode=mode, run_id=run_id)
    used_mode = getattr(client, "mode", "mock")

    budget_decision = None
    if used_mode == "live":
        # §24: пред-проверка бюджета режима masked_smoke на ВЕСЬ набор (5 ролей × N кейсов)
        expected_calls = 5 * n
        budget_decision = RB.precheck(BUDGET_MODE, expected_calls=expected_calls, limits=limits)
        if not budget_decision["allowed"]:
            return {
                "run_id": run_id, "mode": used_mode, "spec_ref": "§23.2(б), §24",
                "ОТКАЗ_бюджет": budget_decision,
                "вывод": f"ОТКАЗ до первого вызова: {budget_decision['reason']}",
                "gate_пройден": False,
            }
        client.cost_guard = RB.RunBudgetGuard(BUDGET_MODE, budget_decision["cap_usd"])

    rubric = D.load_rubric()
    threshold = _masked_threshold(rubric)
    models = OR.load_models()

    # Кейсы НЕЗАВИСИМЫ → гоняем параллельно (внутри кейса дебаты остаются последовательными).
    # Сетевые LLM-вызовы I/O-bound: потоки эффективны. Кап ограничивает одновременные запросы к
    # OpenRouter (защита от 429; у клиента есть ретраи). Логи и cost_guard потокобезопасны.
    max_workers = 1 if used_mode == "mock" else min(MAX_PARALLEL_CASES, len(cases))
    results_map, protocols = {}, {}

    def _run_one(case):
        try:
            return case["id"], evaluate_case(case, client, run_id=run_id, rubric=rubric,
                                             models=models, threshold=threshold)
        except Exception as e:  # упавший кейс = ПРОВАЛ (не «бесплатный» проход; см. _failed_result)
            return case["id"], (_failed_result(case, e, threshold), {"ошибка": f"{type(e).__name__}: {e}"})

    if max_workers <= 1:
        for case in cases:
            cid, (res, proto) = _run_one(case)
            results_map[cid], protocols[cid] = res, proto
    else:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for cid, (res, proto) in ex.map(_run_one, cases):
                results_map[cid], protocols[cid] = res, proto

    results = [results_map[c["id"]] for c in cases]   # стабильный порядок = порядок набора
    agg = ME.aggregate(results)
    summary = {
        "run_id": run_id,
        "mode": used_mode,
        "spec_ref": "§23.2(б) маскированные кейсы; §24 gate ≥70%; §16.6 защита рубрики",
        "масккейс_порог_УСТОЯЛА": threshold,
        "параллелизм_кейсов": max_workers,
        "набор": [r["case_id"] for r in results],
        "пред_проверка_бюджета": budget_decision,
        "кейсы": results,
        "агрегат": agg,
        "gate_пройден": agg["gate_пройден"],
        "честность": ("MOCK — дымовой тест КОНВЕЙЕРА (роли отвечают по форме, не рассуждая); "
                      "доля не является доказательным гейтом качества рассуждения"
                      if used_mode == "mock" else
                      "LIVE — реальное рассуждение контура; маскировка несовершенна (§23.2): "
                      "результат ориентировочный, не доказательный"),
        "вывод": agg["вывод"],
    }
    if write:
        _write_report(summary, protocols)
    return summary


def _write_report(summary, protocols):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    rid = summary["run_id"]
    (REPORTS_DIR / f"{rid}.json").write_text(
        json.dumps({**summary, "протоколы": protocols}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    (REPORTS_DIR / f"{rid}.md").write_text(render_md(summary), encoding="utf-8")


def render_md(summary):
    agg = summary["агрегат"]
    L = [f"# Маскированные кейсы §23.2(б) — {summary['run_id']}", "",
         f"- Режим: **{summary['mode']}** · {summary['честность']}",
         f"- Порог УСТОЯЛА (масккейс, rubric.masked_eval): **{summary.get('масккейс_порог_УСТОЯЛА')}** "
         f"(боевой порог воронки 3.0 не изменён)",
         f"- Кейсов: {agg['n_кейсов']} · зачтено: **{agg['n_зачтено']}** · "
         f"чисто по П8: {agg['n_чисто_П8']}",
         f"- Доля зачтённых: **{agg['доля_зачтено']:.0%}** "
         f"(порог §24 {agg['порог_доли']:.0%}) → "
         f"{'GATE ПРОЙДЕН ✅' if agg['gate_пройден'] else 'GATE НЕ пройден ❌'}",
         f"- Средний % рубрики по affirm-кейсам: {agg['средний_процент_рубрики_affirm']}",
         f"- Оговорка: {agg['оговорка']}", "",
         "| кейс | класс | стойка | исход | балл рубрики | П8 | вопросы §4 | зачёт |",
         "|---|---|---|---|---|---|---|---|"]
    for r in summary["кейсы"]:
        L.append(f"| {r['case_id']} | {r.get('класс_актива','')} | {r['expected_stance']} | "
                 f"{r['verdict_outcome']} | {r.get('rubric_pct')}% | "
                 f"{'✅' if r['p8_clean'] else '❌ '+str(r['p8_violations'])} | "
                 f"{'✅' if r['mandatory_answered'] else '❌'} | "
                 f"{'✅' if r['case_passed'] else '❌'} |")
    L += ["", "## Не зачтённые — почему"]
    any_fail = False
    for r in summary["кейсы"]:
        if not r["case_passed"]:
            any_fail = True
            L.append(f"- **{r['case_id']}** ({r['expected_stance']}): "
                     + "; ".join(r["почему_не_зачтён"]))
    if not any_fail:
        L.append("- (все зачтены)")
    return "\n".join(L) + "\n"
