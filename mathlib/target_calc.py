# -*- coding: utf-8 -*-
"""mathlib/target_calc.py — ВОСПРОИЗВОДИМЫЙ РАСЧЁТ ЦЕЛИ (аудит 07.11, находки №4–5).

Инцидент: цели MP $47.50 и FSLR $230 опирались на исторический ход и чужой таргет, у DHT $27 — на
прибыль одного квартала без связи с ценой акции. Правило: цель = метод + входные числа + формула +
допущения, и КОД пересчитывает формулу. Расчёт, который код не повторил, — не расчёт.

Формула — арифметика над именованными входами: + - * / ** скобки, числа, min/max/abs/round.
Никаких атрибутов, вызовов кроме белого списка, строк. Безопасный разбор через ast.
"""
import ast
import math
import operator
import statistics

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow}
_UN = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FN = {"min": min, "max": max, "abs": abs, "round": round}

METHODS = {
    "dcf": "дисконтированные денежные потоки",
    "nav": "стоимость активов (NAV)",
    "multiple": "мультипликатор к показателю",
    "yield": "доходность выплат (дивиденд/прибыль к цене)",
    "event_analogy": "событийная аналогия (выборка похожих случаев)",
    "external_target": "чужой аналитический таргет",
    "price_level": "ценовой уровень из истории бумаги",
}
# методы, которые сами по себе не обосновывают СОБСТВЕННУЮ оценку стоимости
WEAK_METHODS = {"external_target", "price_level"}
MIN_ANALOGS = 3
TOL = 0.02


class CalcError(ValueError):
    pass


def evaluate(formula, inputs):
    """formula: str, inputs: {name: number} → float (CalcError при любой проблеме)."""
    try:
        tree = ast.parse(str(formula), mode="eval")
    except SyntaxError as e:
        raise CalcError(f"формула не разбирается: {e.msg}")
    vals = {}
    for k, v in (inputs or {}).items():
        try:
            vals[str(k)] = float(v)
        except (TypeError, ValueError):
            raise CalcError(f"вход «{k}» не число: {v!r}")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id not in vals:
                raise CalcError(f"в формуле вход «{n.id}», которого нет среди входных данных")
            return vals[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Div) and b == 0:
                raise CalcError("деление на ноль")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
            return _UN[type(n.op)](ev(n.operand))
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FN
                and not n.keywords):
            return float(_FN[n.func.id](*[ev(a) for a in n.args]))
        raise CalcError(f"недопустимый элемент формулы: {type(n).__name__}")
    out = ev(tree)
    if not math.isfinite(out):
        raise CalcError("результат не конечен")
    return out


def check_basis(basis, target_price):
    """target_basis карточки → {'status': 'обоснована'|'предварительная', 'reasons', 'computed', 'fails'}.
    fails — НЕСХОДИМОСТЬ (заявленный расчёт не повторяется кодом) — это брак, а не «предварительно».
    reasons — почему цель только предварительная (метода нет, метод слабый, аналогий мало)."""
    out = {"status": "предварительная", "reasons": [], "fails": [], "computed": None}
    if not isinstance(basis, dict) or not basis:
        out["reasons"].append("у цели нет расчёта (метод, входные данные, формула)")
        return out
    method = str(basis.get("method") or "").strip()
    if method not in METHODS:
        out["reasons"].append(f"метод цели не указан или неизвестен: {method!r}")
    formula, inputs = basis.get("formula"), basis.get("inputs")
    if formula and isinstance(inputs, dict):
        try:
            val = evaluate(formula, inputs)
            out["computed"] = round(val, 4)
            if target_price and abs(val - float(target_price)) > TOL * abs(float(target_price)):
                out["fails"].append(f"формула цели даёт {val:.2f}, а в карточке цель {float(target_price):g} "
                                    f"(расхождение больше {TOL:.0%}) — расчёт не воспроизводится")
        except CalcError as e:
            out["fails"].append(f"расчёт цели не воспроизводится кодом: {e}")
    else:
        out["reasons"].append("нет формулы с входными числами — расчёт не воспроизвести")
    srcs = basis.get("input_sources") if isinstance(basis.get("input_sources"), dict) else {}
    if isinstance(inputs, dict):
        nosrc = [k for k in inputs if not str(srcs.get(k) or "").strip()]
        if nosrc:
            out["reasons"].append("у входов нет источника/обоснования: " + ", ".join(sorted(nosrc)[:6]))
    if not [a for a in (basis.get("assumptions") or []) if str(a).strip()]:
        out["reasons"].append("допущения расчёта не названы")
    if method in WEAK_METHODS:
        out["reasons"].append(f"метод «{METHODS[method]}» не обосновывает собственную оценку стоимости")
    if method == "event_analogy":
        sample = [s for s in (basis.get("sample") or []) if isinstance(s, dict)]
        moves = []
        for s in sample:
            try:
                moves.append(float(s.get("move_pct")))
            except (TypeError, ValueError):
                pass
        if len(moves) < MIN_ANALOGS:
            out["reasons"].append(f"аналогий с числом хода {len(moves)} < {MIN_ANALOGS} — разброс не оценить")
        else:
            out["analogs"] = {"n": len(moves), "min": min(moves), "median": statistics.median(moves),
                              "max": max(moves)}
        if not str(basis.get("comparability") or "").strip():
            out["reasons"].append("не показано, чем аналогии сопоставимы и чем отличаются")
    if not out["reasons"] and not out["fails"]:
        out["status"] = "обоснована"
    return out
