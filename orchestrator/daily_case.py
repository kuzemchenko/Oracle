# -*- coding: utf-8 -*-
"""orchestrator/daily_case.py — Этап3 (пакет после аудита): АРТЕФАКТ «Разбор дня».

Ежедневный поток кейсов для ТРЕНИРОВКИ СУЖДЕНИЯ владельца (PIVOT_2026-07): один содержательный
кейс, объяснённый до конца. НЕ рекомендация, НЕ прогноз — в predictions.jsonl НИЧЕГО не пишется,
пороги не трогаются (проверяется хуком guard_journal и характеризационными тестами).

Поле СТАТУС гарантирует материал КАЖДЫЙ день (даже в пустой по идеям день есть урок):
  • live_candidate     — свежий кандидат, дошедший до суда сегодня (устоял или ещё без вердикта);
  • candidate_autopsy  — кандидат, которого слепой суд РАЗБИЛ сегодня (вскрытие: почему не взлетело);
  • resolved_postmortem— недавно РАЗРЕШЁННЫЙ прогноз (из outcomes.jsonl): совпал прогноз с фактом?;
  • phenomenon_watch   — феномен под наблюдением (медленный трек, ещё не идея);
  • signal_noise_lesson— то, что ВЫГЛЯДЕЛО сигналом, но отсеялось как шум (урок «сигнал ≠ шум»).

ЖЁСТКОЕ ПРАВИЛО (аудит, критерий успеха): НИКОГДА не повышать статус кейса ради регулярности.
Если в очереди только autopsy и уроки шума — выходят ОНИ, честно помеченные. select_case НЕ
переклеивает ярлык: статус = то, чем кейс является на самом деле.

Обязательные блоки кейса (рендер — bot_reports.format_daily_case, БЕЗ эмодзи):
  1 заголовок-тезис; 2 цепочка рассуждения по порядкам (данные ИЛИ честное «данных нет»);
  3 таблица баллов (6 критериев, неизмеренное = «не измерено», не выдумка);
  4 «кто продаёт и почему неправ» ИЛИ честное «ответа нет»; 5 «идея неверна, если…»;
  6 статус в воронке + причина смерти; 7 вопрос владельцу с вариантами (кормит decisions_user).
Каждый факт — из ПОСЧИТАННОГО прогоном (П8); чего нет — «не измерено».
"""

# live_candidate человеко-метку НЕ держим здесь: она зависит от РЕАЛЬНОГО состояния суда (_LIVE_ЧЕЛОВЕК
# ниже), чтобы не выдать ПРОПУСК/не-судимое за «прошёл суд» (stage-review #7).
СТАТУС_ЧЕЛОВЕК = {
    "candidate_autopsy":   "Вскрытие — эту идею слепой суд разбил сегодня",
    "resolved_postmortem": "Постмортем — разрешённый прогноз, сверяем с фактом",
    "phenomenon_watch":    "Наблюдение — феномен на медленном треке, ещё не идея",
    "signal_noise_lesson": "Урок «сигнал или шум» — что выглядело сигналом, но отсеялось",
}

# Порядок ЦЕННОСТИ для тренировки (не «повышение»: берём самый обучающий РЕАЛЬНО существующий кейс;
# ярлык всегда = что кейс есть на самом деле). resolved_postmortem — самый ценный (факт против
# прогноза), затем живой кандидат, затем вскрытие, наблюдение, урок шума как всегда-доступный низ.
_ПРИОРИТЕТ = ["resolved_postmortem", "live_candidate", "candidate_autopsy",
              "phenomenon_watch", "signal_noise_lesson"]

# Порог СЛАБОЙ НАДЁЖНОСТИ ПЕРЕНОСА (надёжность_r2 = P, что шок доходит по цепочке; НЕ R² цены —
# то изоляция_r2). 0.10 = «доходит реже 1 из 10» → цепочка ненадёжна. Отдельно от mathlib.WEAK_R2
# (тот про одиночный r2); здесь величина другой природы, порог держим локально осознанно.
_ПОРОГ_НАДЁЖНОСТИ_СЛАБ = 0.10


# Состояние состязательного суда по узлу (честность статуса, stage-review #7): live_candidate НЕ
# значит «прошёл суд». Различаем: passed (УСТОЯЛА), skip (ПРОПУСК — исключён ДО суда, нет данных),
# error (ОШИБКА_СУДА — суд не состоялся), untried (суд не гонялся: court=None / нет исхода).
def _court_state(court):
    if not isinstance(court, dict):
        return "untried"
    исход = court.get("исход")
    if исход == "УСТОЯЛА":
        return "passed"
    if исход == "ПРОПУСК":
        return "skip"
    if исход == "ОШИБКА_СУДА":
        return "error"
    if исход in ("РАЗБИТА", "ВЕТО"):
        return "broken"
    return "untried"


_LIVE_ЧЕЛОВЕК = {
    "passed":  "Свежий кандидат — прошёл слепой состязательный суд сегодня",
    "skip":    "Свежая наводка — суд НЕ смог вынести вердикт (нет данных подтвердить движение)",
    "error":   "Свежая наводка — суд не состоялся (сбой контура), это сырая наводка",
    "untried": "Свежая наводка — ещё НЕ прошла состязательный суд (сырая, не вердикт)",
}


def _node_label(sym, name_fn):
    """Тикер → «Имя (ТИКЕР)» человекочитаемо (name_fn инъектируется — тестируемость без БД)."""
    if not sym:
        return "?"
    nm = name_fn(sym) if name_fn else sym
    return sym if nm == sym else f"{nm} ({sym})"


def _chain_from_node(n):
    """Блок 2: цепочка рассуждения по порядкам. Из посчитанных узлов картографа/каскада; где узлов
    нет — честное «данных по звеньям нет» (П8)."""
    узлы = n.get("узлы_каскада") or []
    out = []
    for nd in sorted(узлы, key=lambda x: x.get("порядок") or 0)[:4]:
        out.append({
            "порядок": nd.get("порядок"),
            "узел": (nd.get("узел") or "").strip() or None,
            "чокпоинт": bool(nd.get("чокпоинт")),
            "тикеры": list(nd.get("тикеры") or [])[:3],
        })
    if not out:
        як, order = n.get("якорь"), n.get("порядок")
        if як:
            out.append({"порядок": order if isinstance(order, int) else None,
                        "узел": f"перенос шока от {як} к {n.get('актив')}",
                        "чокпоинт": bool(n.get("чокпоинт")), "тикеры": []})
    return out


def _score_table(n, court):
    """Блок 3: 6 критериев. Значение ИЛИ None (=«не измерено» — честно, не выдумка). Источник —
    только посчитанные поля прогона (порядок, отыгранность, ранг, надёжность, суд)."""
    pr = (n.get("продуктовый_ранг") or {}).get("компоненты") or {}
    priced = n.get("отыгранность_узла")
    if priced is None:
        priced = n.get("отыгранность")
    order = n.get("порядок")
    манип = n.get("манипуляционный_балл")
    if манип is None and isinstance(court, dict):
        манип = court.get("манипуляционный_балл")
    return [
        ("тайминг входа", _timing_word(priced)),
        ("отыгранность рынком", f"{priced:.0%}" if isinstance(priced, (int, float)) else None),
        ("неочевидность", pr.get("неочевидность")),
        ("глубина цепочки последствий", f"{order}-й шаг от события" if isinstance(order, int) else None),
        ("манипуляционный балл", манип),
        ("близость к твоей компетенции", n.get("близость_компетенции")),
    ]


def _timing_word(priced):
    """Отыгранность узла рынком → фаза входа §8 человеческим словом (не голая доля)."""
    if not isinstance(priced, (int, float)):
        return None
    if priced >= 0.66:
        return "ПОЗДНО — рынок уже почти отыграл"
    if priced <= 0.33:
        return "РАНО — рынок почти не отыграл, запас хода есть"
    return "ВОВРЕМЯ — отыгрывается прямо сейчас"


def _who_sells(n, court):
    """Блок 4: «кто продаёт нам и почему он неправ» — из вердикта суда, ИЛИ честное «ответа нет»."""
    if isinstance(court, dict):
        кто = court.get("кто_продаёт_нам") or court.get("кто_против")
        if кто:
            return str(кто).strip()
    return None


def _wrong_if(n, court):
    """Блок 5: «идея неверна, если…» — конкретные условия провала ИЗ посчитанного (П8)."""
    out = []
    order = n.get("порядок")
    rel = n.get("надёжность_r2")     # НАДЁЖНОСТЬ ПЕРЕНОСА по цепочке (P, что шок доходит), НЕ R² цены
    edge = n.get("edge")             # НЕОТЫГРАННЫЙ (оставшийся) ход, на который рассчитываем
    if isinstance(order, int) and order >= 3:
        out.append(f"эффект рассеется по дороге — звено дальнее ({order}-й порядок), связь косвенная")
    if isinstance(rel, (int, float)) and rel < _ПОРОГ_НАДЁЖНОСТИ_СЛАБ:
        out.append(f"цепочка ненадёжна — исторически шок доходит до этого звена лишь ~{rel * 100:.0f}% "
                   "случаев, часто рассеивается по дороге")
    if n.get("провизорный"):
        out.append("связь не подтвердится ценами — пока это гипотеза, а не проверенный перенос")
    if isinstance(edge, (int, float)) and edge:
        out.append(f"неотыгранного хода нет — заявленные ~{edge * 100:+.1f}% на деле уже в цене "
                   "(запас, на который рассчитываем, переоценён)")
    if isinstance(court, dict) and court.get("кто_против"):
        out.append("права «другая сторона»: " + str(court["кто_против"]).strip()[:150])
    if not out:
        out.append("движение не выйдет за пределы обычного шума цены за горизонт оценки")
    return out[:4]


def _funnel_status(status, n, court):
    """Блок 6: статус в воронке + причина смерти/остановки (честно, без сглаживания)."""
    if status == "candidate_autopsy" and isinstance(court, dict):
        балл, порог = court.get("балл"), court.get("порог")
        sc = (f" (балл {балл:.1f} против планки {порог:.0f})"
              if isinstance(балл, (int, float)) and isinstance(порог, (int, float)) else "")
        прим = (" " + str(court.get("примечание")).strip()) if court.get("примечание") else ""
        return f"Отсеяна слепым судом{sc}.{прим}".strip()
    if status == "live_candidate":
        if isinstance(court, dict) and court.get("исход") == "УСТОЯЛА":
            return "Прошла слепой суд, запечатана в форвард-трек честности — сверим прогноз с фактом позже."
        st = _court_state(court)
        if st == "skip":
            return ("Суд НЕ смог вынести вердикт: нет внутридневных данных подтвердить движение "
                    "(ПРОПУСК — самый частый исход контура). До ставки НЕ доведена.")
        if st == "error":
            return "Суд не состоялся (сбой контура) — вердикта нет, наводка сырая. До ставки НЕ доведена."
        return ("Ещё НЕ прогонялась через состязательный суд (это быстрый дневной скан) — "
                "сырая наводка, а не вердикт. До ставки НЕ доведена.")
    if status == "resolved_postmortem":
        return "Прогноз разрешён по стандарту §9 — сверяем запечатанное с фактом."
    if status == "phenomenon_watch":
        return "На медленном треке наблюдения — до торгуемой цепочки ещё не дотянулась."
    if status == "signal_noise_lesson":
        return n.get("причина_отсева") or "Выглядело сигналом, но не прошло порог заметности/FDR — шум."
    return "—"


def _question(status):
    """Блок 7: вопрос владельцу с вариантами — кормит decisions_user.jsonl (петля разметки)."""
    if status == "resolved_postmortem":
        return {"текст": "Твой вердикт постмортему: чему этот исход учит на будущее?",
                "варианты": ["логика подтвердилась", "повезло/не показатель", "логика была неверна"]}
    if status == "candidate_autopsy":
        return {"текст": "Согласен со вскрытием — суд справедливо разбил?",
                "варианты": ["да, идея слабая", "нет, суд ошибся", "нужен глубокий разбор"]}
    if status == "signal_noise_lesson":
        return {"текст": "Твоё чутьё: это был шум или упущенный сигнал?",
                "варианты": ["шум, верно отсеяли", "зря отсеяли", "не знаю, копнуть"]}
    return {"текст": "Как оцениваешь эту цепочку — стоит копать глубже?",
            "варианты": ["убедительно, копнуть", "связь притянута", "мимо интереса"]}


def _headline(status, label, n):
    """Блок 1: заголовок-тезис — суть кейса одной мыслью (не голый тикер)."""
    ev = (n.get("событие") or "").strip()
    if status == "resolved_postmortem":
        факт = n.get("факт_словом") or "результат подведён"
        return f"{label}: прогноз разрешён — {факт}"
    if status == "candidate_autopsy":
        return f"{label}: почему красивая на вид цепочка не выдержала разбора"
    if status == "signal_noise_lesson":
        return f"{label}: как отличить событие от рыночного шума"
    if status == "phenomenon_watch":
        return f"{label}: феномен, за которым слежу — но идеей он ещё не стал"
    # live_candidate: «ещё не отыграл» УТВЕРЖДАЕМ только при ИЗМЕРЕННОЙ низкой отыгранности (stage-review
    # #5: иначе заголовок голословен). Нет измерения → нейтральный заголовок «событие → компания».
    priced = n.get("отыгранность_узла")
    if priced is None:
        priced = n.get("отыгранность")
    if isinstance(priced, (int, float)) and priced <= 0.33:
        head = f"{label}: рынок, похоже, ещё не отыграл движение (отыграно ~{priced:.0%})"
    else:
        head = f"{label}: свежая цепочка от события к компании"
    return (head + f" — повод: {ev[:90]}") if ev else head


def _meaning_for_you(status, label, court=None):
    """Секция «что это значит для тебя» (STYLE_CONTRACT п.3, обязательна) — простыми словами."""
    if status == "live_candidate":
        if _court_state(court) == "passed":
            return (f"Сегодня есть кандидат, ПРОШЕДШИЙ состязательный разбор — {label}. Это ещё не "
                    "рекомендация: разбери логику ниже и реши сам, убеждает ли она тебя.")
        return (f"Сегодня — сырая наводка {label}, ещё НЕ проверенная состязательным судом. Это пища "
                "для размышления, не идея к действию: реши сам, стоит ли копать глубже.")
    if status == "candidate_autopsy":
        return ("Годной к действию идеи сегодня нет — и это честный результат, не сбой. Ниже — "
                "лучший из кандидатов, который система рассмотрела и отклонила. Он полезен как "
                "тренировка: реши сам, согласен ли ты с отказом.")
    if status == "resolved_postmortem":
        return ("Сегодня подводим итог прошлому прогнозу — сверяем, что система обещала, с тем, "
                "что вышло. Это про честность счёта, а не про новую покупку.")
    if status == "phenomenon_watch":
        return ("Покупать нечего — показываю феномен, за которым слежу. До торгуемой идеи он ещё "
                "не дозрел; это пища для ума, а не сигнал.")
    return ("Сегодня — урок про шум. Ниже то, что выглядело как событие, но оказалось случайным "
            "колебанием. Полезно, чтобы твоё чутьё не путало движение с сигналом.")


def _what_to_do(status, n, court):
    """Секция «что делать / чего не делать / что решать» (STYLE_CONTRACT п.6, обязательна)."""
    if status == "live_candidate" and _court_state(court) == "passed":
        return ("Идея прошла состязательный разбор — можно держать её на карандаше и решать, "
                "интересна ли она тебе. Ставку не делаем: вероятности ещё не откалиброваны.")
    if status == "live_candidate":
        return ("Ничего не покупать — наводка сырая, суд её ещё не проверял (или не смог из-за нет "
                "данных). Реши, стоит ли поставить на карандаш для глубокого разбора («разбери ТИКЕР»).")
    if status == "resolved_postmortem":
        return ("Покупать ничего не нужно — реши, чему этот исход тебя учит: подтвердилась логика "
                "или это была случайность. Твоя пометка идёт в счёт честности системы.")
    return ("Ничего не покупать — кейс не дозрел до действия. Реши, стоит ли держать актив "
            "«на карандаше»: если да, система будет следить за появлением подтверждающих данных.")


def _build_case(status, n, court, rid, name_fn, дата=None):
    """Сборка кейса (7 блоков) из узла/идеи + вердикта суда. Данных нет → честные None/«не измерено»."""
    label = _node_label(n.get("актив"), name_fn)
    # live_candidate: человеко-статус зависит от РЕАЛЬНОГО состояния суда (stage-review #7: не выдаём
    # ПРОПУСК/не-судимое за «прошёл суд»). Прочие статусы — фиксированная человеко-метка.
    статус_человек = (_LIVE_ЧЕЛОВЕК.get(_court_state(court), _LIVE_ЧЕЛОВЕК["untried"])
                      if status == "live_candidate" else СТАТУС_ЧЕЛОВЕК.get(status, status))
    return {
        "статус": status,
        "статус_человек": статус_человек,
        "судебное_состояние": _court_state(court) if status == "live_candidate" else None,
        "дата": дата,
        "значит_для_тебя": _meaning_for_you(status, label, court),
        "заголовок": _headline(status, label, n),
        "актив": n.get("актив"),
        "повод": (n.get("событие") or "").strip() or None,
        "цепочка": _chain_from_node(n),
        "баллы": _score_table(n, court),
        "кто_продаёт": _who_sells(n, court),
        "неверна_если": _wrong_if(n, court),
        "статус_воронки": _funnel_status(status, n, court),
        "что_делать": _what_to_do(status, n, court),
        "вопрос": _question(status),
        "тех_id": rid,
    }


def _build_postmortem(rec, rid, name_fn, дата):
    """Постмортем разрешённого прогноза: схема outcomes.jsonl (asset/direction/threshold/observed/
    outcome/probability) → читаемый кейс. Отдельный сборщик — исход это НЕ идея-кандидат, у него
    своя форма (прогноз против факта), а не цепочка-аргументация."""
    asset = rec.get("asset") or rec.get("актив")
    label = _node_label(asset, name_fn)
    dir_word = {"above": "выше", "below": "ниже"}.get(rec.get("direction"), rec.get("direction") or "?")
    thr = rec.get("threshold")
    obs = rec.get("observed_value")
    hit = rec.get("outcome")
    prob = rec.get("probability")
    вердикт = "прогноз СБЫЛСЯ" if hit == 1 else "прогноз НЕ сбылся" if hit == 0 else "исход неоднозначен"
    by = str(rec.get("resolve_by") or "")[:10]
    баллы = [
        ("что прогнозировали", f"{label} {dir_word} {thr}" if thr is not None else f"{label} {dir_word}"),
        ("что вышло по факту", f"{obs}" if obs is not None else None),
        ("исход", вердикт),
        ("заявленная вероятность", f"{prob:.0%}" if isinstance(prob, (int, float)) else None),
        ("срок сверки", by or None),
        ("трек", "провизорный (гипотеза, в деньги не пускали)"
         if str(rec.get("kind", "")).endswith("provisional") else rec.get("kind")),
    ]
    цепочка_txt = (f"Одношаговый форвард-тест: прогноз «{dir_word} {thr}» запечатан заранее к {by}, "
                   "исход сверён с фактом без подглядывания (§16).")
    кто = ("Это форвард-тест связи, а не спор с рынком — «другой стороны» у него нет; ценность в "
           "честном счёте прогноз против факта.")
    if hit == 1:
        неверна = ["один совпавший исход ещё не доказывает калибровку — это счёт, а не победа; "
                   "смотрим на длинной дистанции, не бьётся ли заявленная вероятность с частотой попаданий"]
        воронка = (f"Разрешён по стандарту §9: факт {obs} подтвердил порог {thr} ({dir_word}). "
                   "Запечатан заранее, сверен по времени — это улика честности, не сделка.")
    else:
        неверна = ["если промах системный (заявляли высокую вероятность, а не сбылось) — сигнал, что "
                   "логика переноса слабее заявленного; один промах сам по себе ещё не приговор"]
        воронка = (f"Разрешён по стандарту §9: факт {obs} НЕ дотянул до порога {thr} ({dir_word}). "
                   "Прогноз был запечатан заранее — промах виден честно, не задним числом.")
    return {
        "статус": "resolved_postmortem",
        "статус_человек": СТАТУС_ЧЕЛОВЕК["resolved_postmortem"],
        "дата": дата,
        "значит_для_тебя": _meaning_for_you("resolved_postmortem", label),
        "заголовок": f"{label}: {вердикт} (прогноз «{dir_word} {thr}»)",
        "актив": asset,
        "повод": None,
        "цепочка": [{"порядок": None, "узел": цепочка_txt, "чокпоинт": False, "тикеры": []}],
        "баллы": баллы,
        "кто_продаёт": кто,
        "неверна_если": неверна,
        "статус_воронки": воронка,
        "что_делать": _what_to_do("resolved_postmortem", rec, None),
        "вопрос": _question("resolved_postmortem"),
        "тех_id": rid,
    }


def _candidates_from_protocol(protocol):
    """Все узлы-кандидаты дня с их вердиктом суда: [(node, court_or_None)]. Порядок = продуктовый
    ранг показа, если он проставлен (не влияет на статус, только на выбор среди равных)."""
    g = protocol.get("граф_отбор") or {}
    суд = g.get("суд_money") or {}
    out = []
    for n in (g.get("топ_k") or []):
        out.append((n, суд.get(n.get("актив"))))
    return out


def select_case(protocol, *, outcomes=None, phenomena=None, name_fn=None, now=None):
    """Выбор ОДНОГО кейса дня + честный статус. Возвращает case-dict ЛИБО {"пусто": причина}.

    outcomes  — свежие разрешённые исходы [{актив, ...}] (из outcomes.jsonl; читается снаружи —
                модуль отбора не лезет в журналы). phenomena — феномены медленного трека.
    name_fn   — тикер→имя (инъекция; без БД в тестах). НИКОГДА не повышает статус (аудит-правило).
    """
    rid = protocol.get("run_id", "?")
    дата = _run_date(protocol, now)
    кандидаты = _candidates_from_protocol(protocol)

    пул = {}   # статус → (node, court)  — по одному лучшему на статус
    # resolved_postmortem — из переданных исходов. Берём исход с активом И разрешённый (есть
    # outcome/observed) И СВЕЖИЙ (разрешён не дальше POSTMORTEM_FRESH_DAYS от даты прогона) — иначе
    # один и тот же постмортем «сегодня подводим итог» висел бы много дней и топил живого кандидата
    # (stage-review). Свежесть отсекает застой; несвежий исход → постмортема нет, идём к кандидатам.
    ref = _run_dt(protocol, now)
    _res_out = None
    for o in (outcomes or []):
        if not (o.get("asset") or o.get("актив")):
            continue
        if o.get("outcome") is None and o.get("observed_value") is None:
            continue
        if ref is not None and not _resolved_recently(o, ref):
            continue
        _res_out = o
        break
    if _res_out is not None:
        пул["resolved_postmortem"] = ("__outcome__", _res_out)
    # живой кандидат / вскрытие — по вердикту суда (НЕ переклеиваем: РАЗБИТА = вскрытие, честно)
    for n, court in кандидаты:
        исход = court.get("исход") if isinstance(court, dict) else None
        if исход == "РАЗБИТА" or исход == "ВЕТО":
            пул.setdefault("candidate_autopsy", (n, court))
        else:
            пул.setdefault("live_candidate", (n, court))
    # феномен под наблюдением
    if phenomena:
        пул["phenomenon_watch"] = (phenomena[0], None)
    # урок сигнал/шум — заметные, но отсеянные (кандидат FDR-ярлыка False, либо сырой шум скана)
    lesson = _noise_lesson_node(protocol)
    if lesson:
        пул["signal_noise_lesson"] = (lesson, None)

    for status in _ПРИОРИТЕТ:
        if status in пул:
            n, court = пул[status]
            if status == "resolved_postmortem":
                return _build_postmortem(court, rid, name_fn, дата)   # court здесь = outcome-запись
            return _build_case(status, n, court, rid, name_fn, дата=дата)
    return {"пусто": "сегодня не набралось даже учебного кейса — ни кандидатов, ни исходов, ни урока шума",
            "дата": дата, "тех_id": rid}


POSTMORTEM_FRESH_DAYS = 2   # исход считается «свежим» для показа постмортема ≤2 дней от даты прогона


def _run_dt(protocol, now):
    """datetime прогона (для фильтра свежести исходов): из ts прогона, иначе now (инъекция)."""
    ts = protocol.get("ts")
    if ts:
        try:
            import datetime as _dt
            return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    return now


def _resolved_recently(outcome, ref_dt):
    """Исход разрешён не дальше POSTMORTEM_FRESH_DAYS от даты прогона (по resolved_at/resolve_by)."""
    import datetime as _dt
    for key in ("resolved_at", "resolve_by", "observed_at"):
        v = outcome.get(key)
        if not v:
            continue
        try:
            d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # оба к naive-UTC для сравнения (ref может быть aware/naive)
        rd = ref_dt.replace(tzinfo=None) if ref_dt.tzinfo else ref_dt
        dd = d.replace(tzinfo=None) if d.tzinfo else d
        return abs((rd - dd).days) <= POSTMORTEM_FRESH_DAYS
    return False


_МЕСЯЦЫ = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _run_date(protocol, now):
    """Человеческая дата кейса: из ts прогона, иначе из now (инъекция; без argless-времени)."""
    ts = protocol.get("ts")
    d = None
    if ts:
        try:
            import datetime as _dt
            d = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            d = None
    if d is None and now is not None:
        d = now.date() if hasattr(now, "date") else now
    if d is None:
        return None
    return f"{d.day} {_МЕСЯЦЫ[d.month]}"


def _noise_lesson_node(protocol):
    """Учебный кейс «сигнал/шум»: заметный кандидат скана, который НЕ прошёл строгий FDR (ярлык §15
    False) — показать, почему заметность ≠ реальный сигнал. Из открытого скана (не журнал)."""
    scan = protocol.get("скан") or protocol.get("scan") or {}
    сигналы = scan.get("сигналы") or []
    заметные_но_шум = [s for s in сигналы
                       if s.get("кандидат") and not s.get("сигнал_после_FDR")]
    if not заметные_но_шум:
        return None
    s = sorted(заметные_но_шум, key=lambda x: x.get("p_value") if x.get("p_value") is not None else 1.0)[0]
    sym = s.get("символ")
    ключ = s.get("ключ")
    p = s.get("p_value")
    причина = ("Всплеск заметен, но даже самый сильный из сегодняшних шумовых кандидатов не прошёл "
               "контроль ложных открытий — статистическую проверку с поправкой на то, что система за "
               "день делает сотни замеров (при таком числе проверок случайные всплески неизбежны). "
               "Это фон, а не событие."
               if isinstance(p, (int, float)) and p > 0 else
               "Сигнал заметен глазом, но не прошёл контроль ложных открытий — проверку, что "
               "всплеск не случаен при множестве замеров. Это фон, а не событие.")
    return {"актив": sym or (ключ or "рыночный шум"),
            "событие": (f"всплеск по теме «{ключ}»" if ключ else f"движение {sym}"),
            "провизорный": True, "причина_отсева": причина,
            "узлы_каскада": [], "надёжность_r2": None}
