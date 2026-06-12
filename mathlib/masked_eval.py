# -*- coding: utf-8 -*-
"""mathlib/masked_eval.py — ДЕТЕРМИНИРОВАННАЯ оценка маскированных кейсов §23.2(б).

Инвариант 6 CLAUDE.md: оценка качества — математика, не LLM. Судья выставляет баллы рубрики
(это LLM), но РЕШЕНИЕ «корректно ли рассуждение» и АГРЕГАЦИЯ доли — детерминированный код здесь,
с тестами. Это защищает порог §24 (≥70%) от «протекания» формулировок (§16.6).

Тонкость, которую кодирует этот модуль. Рубрика судьи оценивает МЕРИТ ИДЕИ (сила каскада,
тайминг, манипуляция, разрешимость, net-асимметрия). Качество РАССУЖДЕНИЯ §23.2(б) = верно ли
контур РАЗЛИЧАЕТ реальный каскад и ловушку. Поэтому у кейса есть ожидаемая позиция контура:
  • affirm  — реальный многоступенчатый каскад: верное рассуждение ⟹ судья подтверждает
              (средний балл рубрики ≥ break_threshold);
  • reject  — ловушка/отыгранность (манипуляция, ПОЗДНО): верное рассуждение ⟹ контур НЕ
              сертифицирует идею (РАЗБИТА/ВЕТО). Награждать высоким баллом фабрикацию идеи из
              ловушки нельзя — это нарушало бы П8 («нет данных»/стоп — поощряемый исход).

Смешанный набор (affirm + reject) ловит вырожденные стратегии: «подтверждай всё» проваливает
reject-кейсы, «отвергай всё» — affirm-кейсы. Так «доля корректных рассуждений» честно измеряет
различение, а не настройку под один ответ.

Кейс §23.2(б) ЗАЧТЁН ⟺ ОДНОВРЕМЕННО:
  • рассуждение корректно для типа кейса (affirm: балл ≥ порога; reject: исход РАЗБИТА/ВЕТО);
  • вся цепочка ролей чиста по П8 (ни одного «факт без ссылки / число без расчёта»);
  • отвечены ОБА обязательных вопроса §4 (кто продаёт / почему возможность ещё существует).

Gate §24 / план Нед.8: доля зачтённых кейсов ≥ 0.70 («≥70% кейсов: рассуждение по рубрике
без нарушений П8»).
"""

GATE_FRACTION = 0.70          # §24 / план §19 Нед.8: ≥70% кейсов
DEFAULT_THRESHOLD = 3.0       # rubric.verdict.break_threshold (приемлемо по существу)
DEFAULT_SCALE_MAX = 5.0
_REJECT_OUTCOMES = ("РАЗБИТА", "ВЕТО")


_NO_TRADE = (None, "нет", "no", "none", "", "нет идеи", "no trade")


def _is_no_trade(direction):
    """Отказ контура от сделки (направление «нет»/пусто) — сильнейшая форма «не входить»."""
    if direction is None:
        return True
    return str(direction).strip().lower() in _NO_TRADE


def score_case(case_id, *, expected_stance, rubric_mean, verdict_outcome,
               p8_violations, mandatory_answered, proposed_direction=None,
               threshold=DEFAULT_THRESHOLD, scale_max=DEFAULT_SCALE_MAX,
               masking_imperfect=True, extra=None):
    """Зачёт ОДНОГО кейса с учётом ожидаемой позиции контура (affirm/reject).

    expected_stance: 'affirm' (реальный каскад) | 'reject' (ловушка/поздно).
    rubric_mean: средний балл рубрики судьи (0..scale_max) или None.
    verdict_outcome: исход контура — 'УСТОЯЛА' | 'РАЗБИТА' | 'ВЕТО' | None.
    proposed_direction: итоговое направление, выведенное контуром ('лонг'|'шорт'|'нет'|None).
    p8_violations: суммарное число нарушений П8 во всей цепочке ролей (0 = чисто).
    mandatory_answered: оба обязательных вопроса §4 отвечены.

    Тонкость reject (§23.2б, П8): «контур НЕ сертифицирует ловушку» достигается ДВУМЯ путями —
    идея предложена и РАЗБИТА/ВЕТО, ЛИБО контур сразу ОТКАЗАЛСЯ от сделки (направление «нет») и
    судья подтвердил качество этого ОТКАЗА (УСТОЯЛА оценивает рассуждение-отказ, а не ставку на
    памп). Оба = верное рассуждение. Засчитывать как провал отказ от манипуляции нельзя — «нет
    данных»/стоп поощряется. Сертификация ЖЕ направленной ставки на ловушку (УСТОЯЛА + лонг/шорт) —
    провал.
    affirm: верно ⟺ сертифицирована направленная идея (УСТОЯЛА, балл ≥ порога, направление не «нет»).
    """
    if expected_stance not in ("affirm", "reject"):
        raise ValueError(f"expected_stance ∈ {{affirm, reject}}, дано {expected_stance!r}")
    p8_clean = (p8_violations == 0)
    rubric_pct = round(rubric_mean / scale_max * 100.0, 1) if rubric_mean is not None else None
    no_trade = _is_no_trade(proposed_direction)
    certified_trade = (verdict_outcome == "УСТОЯЛА") and (rubric_mean is not None) \
        and (rubric_mean >= threshold) and (not no_trade)

    if expected_stance == "affirm":
        reasoning_correct = certified_trade
        ожидание = f"сертифицировать направленную идею (балл ≥ {threshold}, УСТОЯЛА, направление≠нет)"
    else:  # reject
        # верно: ловушка НЕ превратилась в сертифицированную направленную ставку
        reasoning_correct = (verdict_outcome in _REJECT_OUTCOMES) or no_trade
        ожидание = "не сертифицировать ловушку (РАЗБИТА/ВЕТО ИЛИ отказ «нет»)"

    case_passed = bool(reasoning_correct and p8_clean and mandatory_answered)
    причины = []
    if not reasoning_correct:
        причины.append(f"рассуждение не соответствует ожиданию [{ожидание}]: "
                       f"исход={verdict_outcome}, средний_балл={rubric_mean}")
    if not p8_clean:
        причины.append(f"нарушения П8 в цепочке: {p8_violations}")
    if not mandatory_answered:
        причины.append("не отвечены обязательные вопросы §4 (кто продаёт / почему существует)")
    return {
        "case_id": case_id,
        "expected_stance": expected_stance,
        "ожидание": ожидание,
        "verdict_outcome": verdict_outcome,
        "rubric_mean": rubric_mean,
        "rubric_pct": rubric_pct,
        "reasoning_correct": reasoning_correct,
        "p8_violations": p8_violations,
        "p8_clean": p8_clean,
        "mandatory_answered": bool(mandatory_answered),
        "case_passed": case_passed,
        "почему_не_зачтён": причины,
        "маскировка_несовершенна": bool(masking_imperfect),
        **(extra or {}),
    }


def aggregate(case_results, *, required_fraction=GATE_FRACTION):
    """Агрегат по набору кейсов → доля зачтённых + gate §24.

    ДВА условия gate (оба обязательны):
      • доля зачтённых ≥ required_fraction (≥0.70);
      • ЖЁСТКАЯ ДИСЦИПЛИНА ЗАЩИТЫ: ВСЕ reject-класс кейсы (ловушки/ПОЗДНО/манипуляции) решены
        верно. Защитная дисциплина не торгуется: сертифицировать ловушку нельзя ни при каком
        проценте. Один проваленный reject → gate НЕ пройден независимо от доли.

    Пустой вход → gate не пройден, нет данных (П8): нельзя «пройти» порог на нуле кейсов."""
    n = len(case_results)
    n_passed = sum(1 for r in case_results if r.get("case_passed"))
    n_p8_clean = sum(1 for r in case_results if r.get("p8_clean"))
    pass_fraction = round(n_passed / n, 4) if n else 0.0
    affirm_pcts = [r["rubric_pct"] for r in case_results
                   if r.get("expected_stance") == "affirm" and r.get("rubric_pct") is not None]
    mean_affirm_pct = round(sum(affirm_pcts) / len(affirm_pcts), 1) if affirm_pcts else None

    rejects = [r for r in case_results if r.get("expected_stance") == "reject"]
    n_reject = len(rejects)
    reject_failed = [r["case_id"] for r in rejects if not r.get("case_passed")]
    reject_discipline_ok = (len(reject_failed) == 0)

    fraction_ok = bool(n > 0 and pass_fraction >= required_fraction)
    gate_passed = bool(fraction_ok and reject_discipline_ok and n_reject > 0)

    if n == 0:
        вывод = "нет кейсов — gate §24 не на чем проверять (П8)"
    elif n_reject == 0:
        вывод = "gate НЕ пройден: в наборе нет reject-кейсов — защитную дисциплину не на чем проверить"
    elif not reject_discipline_ok:
        вывод = (f"gate НЕ пройден: НАРУШЕНА защитная дисциплина — reject-кейсы сертифицированы как "
                 f"ставки: {', '.join(reject_failed)} (доля {pass_fraction:.0%} не спасает)")
    elif not fraction_ok:
        вывод = f"gate НЕ пройден: {n_passed}/{n} ({pass_fraction:.0%}) < {required_fraction:.0%}"
    else:
        вывод = (f"gate §24 ПРОЙДЕН: {n_passed}/{n} ({pass_fraction:.0%}) ≥ {required_fraction:.0%} "
                 f"И все {n_reject} reject-кейсов решены верно")

    return {
        "n_кейсов": n,
        "n_зачтено": n_passed,
        "n_чисто_П8": n_p8_clean,
        "доля_зачтено": pass_fraction,
        "порог_доли": required_fraction,
        "средний_процент_рубрики_affirm": mean_affirm_pct,
        "n_reject": n_reject,
        "reject_провалены": reject_failed,
        "reject_дисциплина_ок": reject_discipline_ok,
        "доля_ок": fraction_ok,
        "gate_пройден": gate_passed,
        "вывод": вывод,
        "оговорка": "маскировка несовершенна (§23.2): результаты ориентировочные, не доказательные",
    }
