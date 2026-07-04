# -*- coding: utf-8 -*-
"""mathlib/attention.py — ДЕТЕРМИНИРОВАННЫЙ датчик перегретости/неочевидности (дорожная карта
«Инвест-партнёр» П1; инвариант 6 — считает КОД, не LLM).

ЗАЧЕМ. Продукт ищет НЕОЧЕВИДНЫЕ идеи, которые ещё НЕ сыграли. Обратная сторона неочевидности —
внимание толпы: как только тему массово ищут («домохозяйка гуглит инструмент»), идея уже
очевидна и, скорее всего, отыграна. Поисковый интерес — измеримый след этого внимания. Здесь
он превращается в ЧИСЛО на оси РАНО↔ПОЗДНО, которое кормит вход тайминга §8 и (далее, П2)
пере-ранжирование потока по «свежести».

ЧТО НА ВХОДЕ. Ряд Google Trends interest_over_time (0–100), где значение — интерес ОТНОСИТЕЛЬНО
собственного максимума окна (Trends само-нормирует пик окна в 100). Поэтому все выводы —
ОТНОСИТЕЛЬНЫЕ (внутри окна ряда), и это честно помечается: тема, впервые пробивающаяся вверх,
на само-нормированной шкале читается как высокая — это ограничение источника, не дефект расчёта.

ЧТО НА ВЫХОДЕ.
  • score (перегретость) ∈ [0,1]: 0 = тема вне радара (свежо/неочевидно), 1 = на пике внимания
    (очевидно/поздно). score = УРОВЕНЬ (сглаженное последнее / 100 — доля собственного пика окна;
    решение 03.07, см. ниже); импульс в величину НЕ входит — он задаёт только фазу.
  • свежесть = 1 − score (удобный множитель для ранжирования «неочевидности»).
  • фаза ∈ {РАНО, ВОВРЕМЯ, ПОЗДНО, ЛОВУШКА, ОТЫГРАНО} — канон тайминга §8 + отдельная метка
    ОТЫГРАНО (REVISION_2026-07 §R4.1: «сдувшийся ДАВНО хайп» — НЕ РАНО и не свежесть).
  • наклон/последний/пик_окна/провенанс/заметка — прозрачность (каждое число объяснимо).

ЧЕСТНОСТЬ (П8). Мало истории, плоский ряд без вариации, пустой вход → score=None с пометкой,
а не выдуманное число. Пороги ниже — КОНСЕРВАТИВНЫЕ ориентиры, не вечная истина: известный
сигнал затухает/арбитражируется, пороги пере-валидируются петлёй качества (§R6), не зашиты навсегда.
"""
import numpy as np

# КАНОНИЧЕСКИЙ timeframe Trends (П1-гейт 04.07, REVISION_2026-07 §R4.1): score = доля
# СОБСТВЕННОГО пика ОКНА, поэтому scores сравнимы только внутри одного timeframe ('today 3-m'
# vs 12-m дают разные «доли пика»). Менять — только вместе с пере-валидацией порогов
# LEVEL_*/MOM_* (§R6-петля); фетч data/trends.py сверяется с этой константой.
TRENDS_TIMEFRAME = "today 3-m"

MIN_HISTORY = 8        # меньше точек — фон внимания не оценить честно (как MIN_TREND_HISTORY скана)
LEVEL_HOT = 0.70       # перцентиль последнего значения ≥ этого → «высоко/на пике внимания»
LEVEL_COLD = 0.40      # ≤ этого → «низко/вне радара»
MOM_UP = 0.15          # нормированный наклон ≥ → «разгорается»
MOM_DN = -0.15         # ≤ → «остывает» (в паре с высоким уровнем = ЛОВУШКА, перекатывается вниз)
NEAR_PEAK_FRAC = 0.70  # «возле пика» = ≥ этой доли пика окна (для отличия хайпа от шум-спайка)
DEFLATION_FRAC = 0.50  # «сдулся» = текущий уровень ≤ этой доли пика окна (падение более чем вдвое)


def _clip01(x):
    return max(0.0, min(1.0, float(x)))


def _clip01_signed(x):
    """Ограничить в [-1, 1] (знаковый импульс)."""
    return max(-1.0, min(1.0, float(x)))


def _empty_result(n, provenance, note, наклон=None):
    """Единый КОНТРАКТ выхода для «нет данных» (кросс-ревью П1-гейта: набор ключей у всех ветвей
    одинаков — потребитель не ловит KeyError на успешном/пустом ответе)."""
    return {"score": None, "свежесть": None, "фаза": None,
            "уровень": None, "наклон": наклон,
            "последний": None, "пик_окна": None, "n": n,
            "окно_trends": TRENDS_TIMEFRAME,
            "провенанс": provenance, "заметка": note}


def _trim_partial(vals, is_partial):
    """Срезать ХВОСТ незавершённых (is_partial) корзин Trends: последняя корзина периода часто
    неполна и читается заниженно → фейковое «остывание/ЛОВУШКА». Режем только непрерывный хвост."""
    if not is_partial:
        return vals, 0
    v = list(vals)
    flags = list(is_partial)
    dropped = 0
    while v and flags and flags[-1]:
        v.pop()
        flags.pop()
        dropped += 1
    return v, dropped


def attention_score(interest, *, is_partial=None, recent=4, min_history=MIN_HISTORY):
    """Датчик перегретости по ряду поискового интереса (Trends 0–100), oldest→newest.

    interest    — список чисел (интерес), по возрастанию даты.
    is_partial  — опц. параллельный список флагов незавершённости корзины (хвост срезается).
    recent      — размер окна импульса (последние N точек vs предыдущие N).

    Возвращает dict (ЕДИНЫЙ набор ключей у всех ветвей): score (перегретость 0..1 | None),
    свежесть, фаза, уровень, наклон, последний, пик_окна, n, окно_trends, провенанс, заметка.
    score=None (П8) при нехватке истории/нулевой вариации.
    """
    # П1-гейт 04.07 (а): значение и его partial-флаг фильтруются ПАРОЙ. Раньше vals чистился
    # от None/non-finite, а is_partial — нет: списки съезжали, и _trim_partial резал полноценные
    # корзины (или НЕ резал партиал → фейковое «остывание»/ЛОВУШКА).
    seq = list(interest or [])
    notes = []
    if is_partial is not None and len(is_partial) != len(seq):
        notes.append("флаги is_partial рассинхронизированы с рядом — игнорированы (дефект вызова)")
        is_partial = None
    flags = list(is_partial) if is_partial is not None else [False] * len(seq)
    pairs = [(float(x), bool(f)) for x, f in zip(seq, flags)
             if x is not None and np.isfinite(x)]
    vals, dropped = _trim_partial([p[0] for p in pairs], [p[1] for p in pairs])
    if dropped:
        notes.append(f"срезан незавершённый хвост Trends: {dropped}")
    note = "; ".join(notes)

    n = len(vals)
    if n < min_history:
        return _empty_result(n, f"мало истории (<{min_history})", note or "нет данных (П8)")

    a = np.asarray(vals, dtype=float)
    latest = a[-1]
    spread = float(np.quantile(a, 0.90) - np.quantile(a, 0.10))
    if spread <= 0:
        spread = float(a.max() - a.min())
    if spread <= 0:
        # плоский ряд: относительной информации о «горячо/холодно» нет — не выдумываем (П8)
        return _empty_result(n, "нулевая вариация ряда (плоский)",
                             (note + "; " if note else "") + "нет относительного сигнала внимания",
                             наклон=0.0)

    prov = []

    # УРОВЕНЬ / ПЕРЕГРЕТОСТЬ — доля СОБСТВЕННОГО пика внимания за окно. Google Trends само-нормирует
    #   максимум окна в 100, поэтому значение/100 = «какую часть своего пика тема занимает СЕЙЧАС»:
    #   14 = холодно (вне радара), 96 = у пика (очевидно/поздно). Это и есть искомая ось перегретости.
    #   ВАЖНО и ЧЕСТНО: НЕ перцентиль внутри ряда — у монотонно растущей темы последнее значение всегда
    #   максимум, и перцентиль ложно показал бы «перегрето» ровно для самой свежей, нарождающейся темы.
    #   Сглаживаем по последним w точкам, чтобы одиночный шум конца не дёргал оценку.
    r = max(2, min(recent, n // 2))
    w = max(2, r // 2)
    smooth_last = float(np.median(a[-w:]))
    peak = float(a.max())
    level = _clip01(smooth_last / 100.0)
    score = round(level, 4)
    prov.append(f"уровень={round(level, 3)} (последнее≈{round(smooth_last, 1)}/100, пик окна={round(peak, 1)})")

    # ИМПУЛЬС — знак/сила НЕДАВНЕГО направления: медиана последних w vs предыдущих w точек,
    #    нормировано на собственный разброс → безразмерно. Локально (w мало), чтобы честно ловить
    #    свежий разворот, а не сглаживать его длинным окном. Не входит в величину перегретости — задаёт ФАЗУ.
    if n >= 2 * w:
        momentum = _clip01_signed((float(np.median(a[-w:])) - float(np.median(a[-2 * w:-w]))) / spread)
    else:
        momentum = _clip01_signed((latest - float(a[0])) / spread)
    prov.append(f"импульс={round(momentum, 3)}")

    # Откат с НЕДАВНЕГО пика: тема взлетела в пределах последних ~r точек и заметно сдулась.
    # Кросс-ревью П1-гейта (HIGH): argmax берёт ПЕРВОЕ вхождение максимума — плато пика
    # ([...100,100,100,35...]) читалось бы «пик давно» → ложное ОТЫГРАНО вместо ЛОВУШКИ.
    # Давность пика меряем от ПОСЛЕДНЕГО касания максимума.
    peak_idx = n - 1 - int(np.argmax(a[::-1]))
    drawdown = (latest - float(a[peak_idx])) / spread          # ≤ 0
    recent_peak = (n - 1 - peak_idx) <= max(2, r)
    post_peak = recent_peak and drawdown <= MOM_DN

    # «Сдувшийся ДАВНО хайп» (П1-гейт 04.07 (в), REVISION_2026-07 §R4.1). Раньше тема, отхайпившая
    # много корзин назад и упавшая ниже LEVEL_COLD, читалась как РАНО с максимальной «свежестью» —
    # инверсия сигнала на целевом классе: это ОТЫГРАННЫЙ сюжет, противоположный «ещё не сработавшему».
    # Отличаем хайп от шум-спайка само-нормировки двумя детерминированными признаками:
    #   • устойчивость: ≥2 СМЕЖНЫХ точек «возле пика» (≥ NEAR_PEAK_FRAC × пик) — stage-review H-1:
    #     два ИЗОЛИРОВАННЫХ выброса (типичный шум разреженных рядов нишевых ключей — ровно целевой
    #     класс «неочевидных» тем) хайпом не считаются;
    #   • глубина сдутия: текущий сглаженный уровень ≤ DEFLATION_FRAC × пик (упало более чем вдвое) —
    #     шумный низкоинтересный ряд со случайным ранним argmax так не «сдувается».
    near = a >= NEAR_PEAK_FRAC * peak
    max_run = run = 0
    for f in near:
        run = run + 1 if f else 0
        max_run = max(max_run, run)
    old_peak_deflated = ((not recent_peak) and level <= LEVEL_COLD
                         and smooth_last <= DEFLATION_FRAC * peak and max_run >= 2)

    # ФАЗА (детерминированно): канон §8 + метка ОТЫГРАНО (§R4.1). Порядок: «ещё горячо» →
    # «сдулось недавно» → «сдулось давно» → «вне радара».
    if level >= LEVEL_HOT:
        if momentum >= MOM_UP:
            phase = "ВОВРЕМЯ"        # высоко, но ещё ускоряется — толпа заходит, возможно догоняемо
        elif momentum <= MOM_DN:
            phase = "ЛОВУШКА"        # высоко и уже перекатывается вниз — пик проходит
        else:
            phase = "ПОЗДНО"         # высоко и плато — все уже здесь, насыщение
    elif post_peak:
        phase = "ЛОВУШКА"           # недавно был пик, откатился — хайп сдулся (поздно догонять)
    elif old_peak_deflated:
        phase = "ОТЫГРАНО"          # хайп был давно и сдулся — НЕ «рано», сюжет уже отыгран
    elif level <= LEVEL_COLD:
        phase = "РАНО"              # вне радара БЕЗ следа отгремевшего хайпа — неочевидно
    else:
        phase = "ВОВРЕМЯ"

    return {"score": score, "свежесть": round(1.0 - score, 4), "фаза": phase,
            "уровень": round(level, 4), "наклон": round(momentum, 4),
            "последний": round(smooth_last, 1), "пик_окна": round(peak, 1), "n": n,
            "окно_trends": TRENDS_TIMEFRAME,     # канонический параметр (scores сравнимы внутри окна)
            "провенанс": "; ".join(prov),
            "заметка": note or "интерес Trends само-нормирован к пику окна (выводы относительные)"}


def attention_from_rows(rows, asof=None, max_age_days=None, **opts):
    """Удобный вход из строк БД одного ключа: rows = [(date, interest[, is_partial[, fetched_at]]), ...].
    Сортирует по дате, извлекает ряд и флаги partial, зовёт attention_score.

    П1-гейт 04.07 (б) — ЛОСКУТНАЯ РЕ-НОРМИРОВКА: data/trends.py пишет INSERT OR REPLACE по
    (keyword,geo,date) → в БД копится ряд, где старые даты нормированы к пикам СТАРЫХ окон Trends,
    свежие — к новому пику. Импульс/пик/фаза через швы нормировок дают ложные развороты
    (ЛОВУШКА/РАНО — артефакт склейки). Если строки несут fetched_at (4-й элемент; используйте
    data/trends.rows_for_attention) — расчёт идёт ТОЛЬКО по последнему фетчу: одна нормировка.

    ЧЕСТНОСТЬ УСТАРЕВАНИЯ (кросс-ревью, HIGH): пустой/упавший свежий фетч не пишет строк — MAX
    (fetched_at) молча вернёт СТАРЫЙ ряд. Поэтому: (1) fetched_at использованного фетча всегда
    в выдаче («фетч_utc» — провенанс П8); (2) при asof (ISO UTC момента прогона) + max_age_days
    устаревший фетч даёт score=None («фетч устарел»), а не фазу по старой нормировке."""
    clean = []
    for row in rows or []:
        date = row[0]
        interest = row[1] if len(row) > 1 else None
        part = row[2] if len(row) > 2 else None
        fetched = row[3] if len(row) > 3 else None
        clean.append((date, interest, part, fetched))
    fetches = {c[3] for c in clean if c[3] is not None}
    last_fetch = max(fetches) if fetches else None
    if last_fetch is not None:
        clean = [c for c in clean if c[3] == last_fetch]   # одна нормировка — без швов
    stale_check_note = None
    if last_fetch is not None and asof is not None and max_age_days is not None:
        import datetime as _dt
        try:
            f_dt = _dt.datetime.fromisoformat(str(last_fetch).replace("Z", "+00:00"))
            a_dt = _dt.datetime.fromisoformat(str(asof).replace("Z", "+00:00"))
            if (a_dt - f_dt).total_seconds() > float(max_age_days) * 86400:
                res = _empty_result(len(clean), f"фетч устарел (> {max_age_days} дн от asof)",
                                    "старый ряд не выдаём за текущий сигнал (П8)")
                res["фетч_utc"] = last_fetch
                return res
        except ValueError:
            # stage-review L-4 (П8): отказ проверки честности обязан быть ВИДЕН, не молчать
            stale_check_note = "staleness-проверка ПРОПУЩЕНА: нечитаемые метки fetched_at/asof"
    clean.sort(key=lambda x: x[0])
    interest = [r[1] for r in clean]
    is_partial = [bool(r[2]) for r in clean] if any(r[2] is not None for r in clean) else None
    res = attention_score(interest, is_partial=is_partial, **opts)
    res["фетч_utc"] = last_fetch                            # провенанс: по какому фетчу посчитано
    if stale_check_note:
        res["заметка"] = ((res.get("заметка") or "") + "; " if res.get("заметка") else "") + stale_check_note
    return res


def attention_map(trends_rows, **opts):
    """Датчик по КАЖДОМУ ключу. trends_rows = [(keyword, date, interest[, is_partial[, fetched_at]]), ...]
    (форма как в event_scan.trend_signals + опц. флаги). Возвращает {keyword: attention_score}."""
    by_kw = {}
    for row in trends_rows or []:
        keyword = row[0]
        by_kw.setdefault(keyword, []).append(row[1:])
    return {keyword: attention_from_rows(rows, **opts) for keyword, rows in by_kw.items()}
