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


def avg_call_cost(costs_log, *, month=None, prior=0.04):
    """Средняя цена УСПЕШНОГО live-вызова за месяц из journal/costs.jsonl.

    Источник правды стоимости — журнал реальных вызовов (решение Нед.1). Пока успешных
    live-строк нет — возвращаем консервативный приор (basis='приор'). Mock-строки (cost 0)
    и неуспешные — не считаем."""
    p = pathlib.Path(costs_log)
    month = month or _month()
    costs = []
    if p.exists():
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
                costs.append(float(c))
    if costs:
        return sum(costs) / len(costs), "история", len(costs)
    return float(prior), "приор", 0


def estimate_run_cost(mode, *, expected_calls=None, limits=None, costs_log=None, month=None):
    """Оценка стоимости прогона ДО вызовов = ожидаемое_число_вызовов × средняя_цена_вызова."""
    lim = limits or L.load_limits()
    costs_log = costs_log or (ROOT / lim["budget"].get("costs_log", "journal/costs.jsonl"))
    if expected_calls is None:
        expected_calls = (lim.get("per_run_expected_calls", {}) or {}).get(mode)
    if expected_calls is None:
        raise ValueError(f"нет per_run_expected_calls для режима {mode!r} и не передан expected_calls")
    prior = float(lim.get("cost_per_call_prior_usd", 0.04))
    avg, basis, n_hist = avg_call_cost(costs_log, month=month, prior=prior)
    est = round(expected_calls * avg, 4)
    return {"estimate_usd": est, "expected_calls": expected_calls,
            "avg_call_usd": round(avg, 5), "basis_avg": basis, "n_history": n_hist}


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
                          f"{est['expected_calls']}×${est['avg_call_usd']} ({est['basis_avg']})]"}

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
        self._lock = threading.Lock()

    def add(self, cost_usd):
        with self._lock:
            self.calls += 1
            if isinstance(cost_usd, (int, float)):
                self.spent_usd += float(cost_usd)
            exceeded = self.spent_usd >= self.cap_usd
            spent = self.spent_usd
        if exceeded:
            raise RunBudgetExceeded(self.mode, spent, self.cap_usd)

    def __call__(self, cost_usd):  # удобно передавать сам объект как callback
        self.add(cost_usd)
