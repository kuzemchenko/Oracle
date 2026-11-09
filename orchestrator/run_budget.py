# -*- coding: utf-8 -*-
"""orchestrator/run_budget.py — ПРЕД-проверка бюджета токенов ПЕРЕД live-прогоном.

Закрытие долга Нед.8 (см. [[week7-adversarial-synthesis]]): «проверка per_run_token_budget
ПЕРЕД live-прогоном — сейчас стоимость только постфактум». MASTER_SPEC §24: «Бюджет токенов
фиксируется на каждый шаг заранее (limits.yaml); превышение — стоп и разбор». Инвариант 5
CLAUDE.md: лимиты в коде, обсуждать превышение в моменте — отказ.

Два контура (оба обязательны для live):
  1. ПРЕД-оценка (precheck): ДО первого LLM-вызова считаем оценку прогона =
     ожидаемое_число_вызовов × средняя_цена_вызова (из истории costs.jsonl или приора).
     Оценка > потолка режима ИЛИ месячный потолок уже достигнут → ОТКАЗ, ни одного вызова.
  2. СТОП на лету (RunBudgetGuard): по ходу прогона суммируем реальную стоимость; пересечение
     потолка режима → исключение, прогон обрывается (защита, если оценка занизила).

Оценка — это математика на журнале, не LLM. Решение поднять потолок — только пользователь
правкой config/limits.yaml (П12), не предмет уговоров.
"""
import datetime
import json
import pathlib

import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mathlib import limits as L  # noqa: E402


class RunBudgetRefused(RuntimeError):
    """Пред-проверка отказала: прогон не должен начинаться (§24)."""
    def __init__(self, decision):
        super().__init__(decision["reason"])
        self.decision = decision


class RunBudgetExceeded(BaseException):
    """Стоп на лету: реальная стоимость прогона пересекла потолок режима (§24).

    Долг[HIGH] из stage-review F0: НАСЛЕДУЕТ BaseException (как KeyboardInterrupt/SystemExit), а НЕ
    Exception — иначе широкие `except Exception` в контуре (openrouter.complete фолбэк-цикл,
    agents.call_agent, event_first._vet_money/_deep, funnel-стадии) ГЛОТАЛИ сигнал стопа → прогон
    продолжал тратить сверх потолка. Теперь сигнал не перехватывается обработчиками ошибок и
    долетает до входа прогона (run_funnel/run_event_first/masked), где ловится ЯВНО и даёт
    graceful-остановку. Это управляющее исключение, не «ошибка вызова»."""
    def __init__(self, mode, spent_usd, cap_usd):
        super().__init__(f"прогон '{mode}': потрачено ${spent_usd:.4f} ≥ потолка ${cap_usd} (§24) — стоп")
        self.mode, self.spent_usd, self.cap_usd = mode, spent_usd, cap_usd


def _month(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m")


# VQ-4 (09.11): цена вызова считается ПО РЕЖИМУ. Раньше бралась средняя по ВСЕМ вызовам месяца —
# туда попадали дорогие ответы Telegram-агента (≈$0.23 за вызов против ≈$0.06 у утреннего прогона),
# и 120 ожидаемых вызовов утреннего прогона «стоили» $12.4 > потолка $10 → отказ 08.11, хотя реальные
# прогоны 27.10–07.11 стоили $2.7–4.2. Число вызовов (план) НЕ уменьшаем — меняем только цену вызова
# на честную цену ЭТОГО режима. Нет своей истории → средняя по фоновым контурам (без разговоров) → приор.
MODE_RUN_PREFIX = {
    "event_first": ("ef_",),
    "challenge": ("challenge",),
    "week_odds": ("wo_",),
    "hunter_court": ("hunter",),
    "story_editor": ("story",),
    "world_map": ("wm_", "world"),
}
INTERACTIVE_PREFIXES = ("bot",)      # разговоры владельца — не прогон, их цена к прогону не относится
MIN_MODE_HISTORY = 20                # меньше вызовов режима за месяц — своей средней не доверяем


def _cost_rows(costs_log, month):
    p = pathlib.Path(costs_log)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("mode") == "mock" or not rec.get("ok"):
            continue
        if not str(rec.get("ts", "")).startswith(month):
            continue
        c = rec.get("cost_usd")
        if isinstance(c, (int, float)) and c > 0:
            out.append(rec)
    return out


def _rid(rec):
    return str(rec.get("run_id") or "")


def avg_call_cost(costs_log, *, month=None, prior=0.04, mode=None):
    """Средняя цена УСПЕШНОГО live-вызова за месяц из journal/costs.jsonl.

    mode задан и у режима есть ≥MIN_MODE_HISTORY своих вызовов → средняя ЭТОГО режима (basis='режим').
    Иначе → средняя фоновых контуров без разговоров владельца (basis='история'). Нет строк → приор."""
    month = month or _month()
    rows = _cost_rows(costs_log, month)
    pref = MODE_RUN_PREFIX.get(mode or "")
    if pref:
        own = [r["cost_usd"] for r in rows if _rid(r).startswith(pref)]
        if len(own) >= MIN_MODE_HISTORY:
            return sum(own) / len(own), "режим", len(own)
    bg = [r["cost_usd"] for r in rows if not _rid(r).startswith(INTERACTIVE_PREFIXES)]
    if bg:
        return sum(bg) / len(bg), "история", len(bg)
    return float(prior), "приор", 0


def recent_runs(mode, costs_log, *, month=None, last=10):
    """Фактические прогоны режима: [(run_id, вызовов, $)] за месяц — контроль, что оценка не оторвалась."""
    pref = MODE_RUN_PREFIX.get(mode or "")
    if not pref:
        return []
    agg = {}
    for r in _cost_rows(costs_log, month or _month()):
        rid = _rid(r)
        if rid.startswith(pref):
            run = rid.split("__")[0]
            n, c = agg.get(run, (0, 0.0))
            agg[run] = (n + 1, c + float(r["cost_usd"]))
    return [(k, n, round(c, 4)) for k, (n, c) in sorted(agg.items())][-last:]


def estimate_run_cost(mode, *, expected_calls=None, limits=None, costs_log=None, month=None):
    """Оценка стоимости прогона ДО вызовов = ожидаемое_число_ВЫЗОВОВ_моделей × цена_вызова_режима."""
    lim = limits or L.load_limits()
    costs_log = costs_log or (ROOT / lim["budget"].get("costs_log", "journal/costs.jsonl"))
    if expected_calls is None:
        expected_calls = (lim.get("per_run_expected_calls", {}) or {}).get(mode)
    if expected_calls is None:
        raise ValueError(f"нет per_run_expected_calls для режима {mode!r} и не передан expected_calls")
    prior = float(lim.get("cost_per_call_prior_usd", 0.04))
    avg, basis, n_hist = avg_call_cost(costs_log, month=month, prior=prior, mode=mode)
    est = round(expected_calls * avg, 4)
    out = {"estimate_usd": est, "expected_calls": expected_calls,
           "avg_call_usd": round(avg, 5), "basis_avg": basis, "n_history": n_hist}
    runs = recent_runs(mode, costs_log, month=month)
    if runs:
        out["факт_прогонов"] = {"n": len(runs), "max_usd": max(c for _, _, c in runs),
                                "max_вызовов": max(n for _, n, _ in runs)}
    return out


def fit_plan(stages, cap_usd, avg_call_usd):
    """Урезать НЕобязательный объём под потолок, сохранив обязательные этапы целиком.

    stages: [{"этап": str, "вызовов": int, "обязательный": bool}] — план работ по этапам (вызовы моделей).
    → {"влезает": bool, "этапы": [... с "разрешено"], "вызовов": int, "отложено": {этап: вызовов}}.
    Обязательное не влезает целиком → влезает=False (прогон не начинать, ничего не урезать молча)."""
    must = sum(s["вызовов"] for s in stages if s.get("обязательный"))
    room = cap_usd / avg_call_usd if avg_call_usd > 0 else float("inf")
    if must > room:
        return {"влезает": False, "вызовов": must, "отложено": {},
                "причина": f"обязательные этапы {must} вызовов × ${avg_call_usd:.4f} > потолка ${cap_usd}"}
    left = int(room - must)
    out, deferred, total = [], {}, must
    for s in stages:
        s2 = dict(s)
        if s.get("обязательный"):
            s2["разрешено"] = s["вызовов"]
        else:
            s2["разрешено"] = min(s["вызовов"], max(0, left))
            left -= s2["разрешено"]
            total += s2["разрешено"]
            if s2["разрешено"] < s["вызовов"]:
                deferred[s["этап"]] = s["вызовов"] - s2["разрешено"]
        out.append(s2)
    return {"влезает": True, "этапы": out, "вызовов": total, "отложено": deferred}


def precheck(mode, *, expected_calls=None, limits=None, costs_log=None, month=None):
    """ПРЕД-проверка бюджета прогона (§24). Возвращает решение; allowed=False = ОТКАЗ.

    Проверяет три вещи ДО любого вызова:
      (1) месячный потолок владения уже достигнут → стоп всех прогонов (§30 п.2);
      (2) оценка прогона > потолка режима per_run_token_budget_usd[mode] (§24);
      (3) месячный_спенд + оценка > месячного потолка → отказ (не начинаем то, что не влезет).
    """
    lim = limits or L.load_limits()
    costs_log = costs_log or (ROOT / lim["budget"].get("costs_log", "journal/costs.jsonl"))
    est = estimate_run_cost(mode, expected_calls=expected_calls, limits=lim,
                            costs_log=costs_log, month=month)
    # источник правды месячного спенда — сумма journal/costs.jsonl (решение Нед.1)
    from ops.budget import oracle_monthly_spend  # noqa: E402
    spent_month, _, _ = oracle_monthly_spend(costs_log, month=month)

    base = {"mode": mode, **est, "spent_month_usd": round(spent_month, 4),
            "month_cap_usd": lim["budget"]["total_usd_month"]}

    # (1) месячный потолок уже достигнут
    mb = L.check_monthly_budget(spent_month, limits=lim)
    if not mb["allowed"]:
        return {**base, "allowed": False, "контур": "месячный_потолок",
                "reason": f"ОТКАЗ (§30 п.2): {mb['reason']}; новые прогоны стоп до решения пользователя"}

    # (2) оценка против потолка режима
    rb = L.check_run_token_budget(mode, est["estimate_usd"], limits=lim)
    if not rb["allowed"]:
        return {**base, "allowed": False, "контур": "потолок_режима", "cap_usd": rb.get("limit"),
                "reason": f"ОТКАЗ (§24): {rb['reason']} [оценка={est['estimate_usd']} = "
                          f"{est['expected_calls']} вызовов моделей×${est['avg_call_usd']} ({est['basis_avg']})]"}

    # (3) месячный спенд + оценка влезает в месячный потолок
    would_be = spent_month + est["estimate_usd"]
    if would_be > lim["budget"]["total_usd_month"]:
        return {**base, "allowed": False, "контур": "месячный_потолок", "would_be_usd": round(would_be, 4),
                "reason": (f"ОТКАЗ (§30 п.2): спенд ${spent_month:.2f} + оценка ${est['estimate_usd']:.2f} "
                           f"= ${would_be:.2f} > потолка ${lim['budget']['total_usd_month']}/мес")}

    return {**base, "allowed": True, "cap_usd": rb.get("limit"),
            "reason": (f"ОК (§24): оценка ${est['estimate_usd']} ≤ потолка режима ${rb.get('limit')}; "
                       f"месяц ${spent_month:.2f} + оценка влезает в ${lim['budget']['total_usd_month']}")}


def precheck_or_raise(mode, **kw):
    """Пред-проверка; при отказе бросает RunBudgetRefused (прогон не начинается)."""
    d = precheck(mode, **kw)
    if not d["allowed"]:
        raise RunBudgetRefused(d)
    return d


class RunBudgetGuard:
    """Стоп на лету: суммирует реальную стоимость прогона, рвёт при пересечении потолка режима.

    Передаётся live-клиенту как cost_guard: после каждого успешного вызова вызывается add(cost).
    Защита второго эшелона — если пред-оценка занизила. Mock не тратит → guard не срабатывает.
    Потокобезопасен: при параллельном прогоне кейсов add() зовётся из разных потоков (§24)."""
    def __init__(self, mode, cap_usd):
        import threading
        self.mode = mode
        self.cap_usd = float(cap_usd)
        self.spent_usd = 0.0
        self.calls = 0
        self.unaccounted_calls = 0   # M7: вызовы без cost — видимы, не «бесплатны»
        self.estimated_usd = 0.0     # сколько начислено оценкой (для честного протокола)
        self._known_usd = 0.0
        self._known_calls = 0
        self._lock = threading.Lock()

    # Прайор стоимости вызова без usage.cost (кросс-ревью ночи 04.07: None-вызовы были
    # БЕСПЛАТНЫ для стопа-на-лету — 100 вызовов при cap=$1 не рвали прогон). Начисляем
    # скользящее среднее ИЗВЕСТНЫХ стоимостей прогона, а до первого известного — консервативный
    # прайор (наблюдаемый вызов судьи ~$0.07; берём с запасом).
    PRIOR_CALL_USD = 0.10

    def add(self, cost_usd):
        with self._lock:
            self.calls += 1
            if isinstance(cost_usd, (int, float)):
                self.spent_usd += float(cost_usd)
                self._known_usd += float(cost_usd)
                self._known_calls += 1
            else:
                # M7: вызов без стоимости виден И платен для лимита (оценка, честно помечена)
                self.unaccounted_calls += 1
                est = (self._known_usd / self._known_calls) if self._known_calls else self.PRIOR_CALL_USD
                self.spent_usd += est
                self.estimated_usd += est
            exceeded = self.spent_usd >= self.cap_usd
            spent = self.spent_usd
        if exceeded:
            raise RunBudgetExceeded(self.mode, spent, self.cap_usd)

    def __call__(self, cost_usd):  # удобно передавать сам объект как callback
        self.add(cost_usd)
