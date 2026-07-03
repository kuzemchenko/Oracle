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
    (очевидно/поздно). Среднее ДОСТУПНЫХ компонент (уровень + аномалия), как в behavioral.
  • свежесть = 1 − score (удобный множитель для ранжирования «неочевидности»).
  • фаза ∈ {РАНО, ВОВРЕМЯ, ПОЗДНО, ЛОВУШКА} — канон тайминга §8, из (уровень, импульс).
  • компоненты/наклон/провенанс/заметка — прозрачность (каждое число объяснимо).

ЧЕСТНОСТЬ (П8). Мало истории, плоский ряд без вариации, пустой вход → score=None с пометкой,
а не выдуманное число. Пороги ниже — КОНСЕРВАТИВНЫЕ ориентиры, не вечная истина: известный
сигнал затухает/арбитражируется, пороги пере-валидируются петлёй качества (§R6), не зашиты навсегда.
"""
import numpy as np

MIN_HISTORY = 8        # меньше точек — фон внимания не оценить честно (как MIN_TREND_HISTORY скана)
LEVEL_HOT = 0.70       # перцентиль последнего значения ≥ этого → «высоко/на пике внимания»
LEVEL_COLD = 0.40      # ≤ этого → «низко/вне радара»
MOM_UP = 0.15          # нормированный наклон ≥ → «разгорается»
MOM_DN = -0.15         # ≤ → «остывает» (в паре с высоким уровнем = ЛОВУШКА, перекатывается вниз)


def _clip01(x):
    return max(0.0, min(1.0, float(x)))


def _clip01_signed(x):
    """Ограничить в [-1, 1] (знаковый импульс)."""
    return max(-1.0, min(1.0, float(x)))


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

    Возвращает dict: score (перегретость 0..1 | None), свежесть, фаза, компоненты, наклон,
    уровень, n, провенанс, заметка. score=None (П8) при нехватке истории/нулевой вариации.
    """
    vals = [float(x) for x in (interest or []) if x is not None and np.isfinite(x)]
    vals, dropped = _trim_partial(vals, is_partial)
    note = f"срезан незавершённый хвост Trends: {dropped}" if dropped else ""

    n = len(vals)
    if n < min_history:
        return {"score": None, "свежесть": None, "фаза": None, "компоненты": {},
                "наклон": None, "уровень": None, "n": n,
                "провенанс": f"мало истории (<{min_history})",
                "заметка": note or "нет данных (П8)"}

    a = np.asarray(vals, dtype=float)
    latest = a[-1]
    spread = float(np.quantile(a, 0.90) - np.quantile(a, 0.10))
    if spread <= 0:
        spread = float(a.max() - a.min())
    if spread <= 0:
        # плоский ряд: относительной информации о «горячо/холодно» нет — не выдумываем (П8)
        return {"score": None, "свежесть": None, "фаза": None, "компоненты": {},
                "наклон": 0.0, "уровень": None, "n": n,
                "провенанс": "нулевая вариация ряда (плоский)",
                "заметка": (note + "; " if note else "") + "нет относительного сигнала внимания"}

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
    peak_idx = int(np.argmax(a))
    drawdown = (latest - float(a[peak_idx])) / spread          # ≤ 0
    post_peak = (n - 1 - peak_idx) <= max(2, r) and drawdown <= MOM_DN

    # ФАЗА на канонической оси §8 (детерминированно). Порядок: сначала «ещё горячо», затем «сдулось».
    if level >= LEVEL_HOT:
        if momentum >= MOM_UP:
            phase = "ВОВРЕМЯ"        # высоко, но ещё ускоряется — толпа заходит, возможно догоняемо
        elif momentum <= MOM_DN:
            phase = "ЛОВУШКА"        # высоко и уже перекатывается вниз — пик проходит
        else:
            phase = "ПОЗДНО"         # высоко и плато — все уже здесь, насыщение
    elif post_peak:
        phase = "ЛОВУШКА"           # недавно был пик, откатился — хайп сдулся (поздно догонять)
    elif level <= LEVEL_COLD:
        phase = "РАНО"              # вне радара — неочевидно
    else:
        phase = "ВОВРЕМЯ"

    return {"score": score, "свежесть": round(1.0 - score, 4), "фаза": phase,
            "уровень": round(level, 4), "наклон": round(momentum, 4),
            "последний": round(smooth_last, 1), "пик_окна": round(peak, 1), "n": n,
            "провенанс": "; ".join(prov),
            "заметка": note or "интерес Trends само-нормирован к пику окна (выводы относительные)"}


def attention_from_rows(rows, **opts):
    """Удобный вход из строк БД одного ключа: rows = [(date, interest[, is_partial]), ...].
    Сортирует по дате, извлекает ряд и флаги partial, зовёт attention_score."""
    clean = []
    for row in rows or []:
        date = row[0]
        interest = row[1] if len(row) > 1 else None
        part = row[2] if len(row) > 2 else None
        clean.append((date, interest, part))
    clean.sort(key=lambda x: x[0])
    interest = [r[1] for r in clean]
    is_partial = [bool(r[2]) for r in clean] if any(r[2] is not None for r in clean) else None
    return attention_score(interest, is_partial=is_partial, **opts)


def attention_map(trends_rows, **opts):
    """Датчик по КАЖДОМУ ключу. trends_rows = [(keyword, date, interest[, is_partial]), ...]
    (форма как в event_scan.trend_signals + опц. флаг partial). Возвращает {keyword: attention_score}."""
    by_kw = {}
    for row in trends_rows or []:
        keyword = row[0]
        by_kw.setdefault(keyword, []).append(row[1:])
    return {keyword: attention_from_rows(rows, **opts) for keyword, rows in by_kw.items()}
