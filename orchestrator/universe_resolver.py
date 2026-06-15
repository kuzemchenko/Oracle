# -*- coding: utf-8 -*-
"""orchestrator/universe_resolver.py — БРАНДМАУЭР «открытие vs запечатывание» (Этап 0, PLAN_cascade_first.md).

Канон: §6 (скан по событиям, не по тикерам), §9 (разрешимость прогноза), §14 (универсум —
по свойствам, не списком), §17.2 (события 1–4 порядка), П16 (форвард-онли запечатывание).

Различаем ТРИ разных множества, которые раньше путались в одном хардкоде `context.CORE`:

  • РАЗ — пространство ОТКРЫТИЯ (discovery). НЕ ограничено никаким списком тикеров: скан событий
    и построение каскадов думают о любом эффекте в мире (§6 Эт.1, §17.2). См. discovery_is_open().

  • ДВА — CALIBRATION_SEED. Курируемый набор инструментов, по которым уже набрана история и
    посчитаны фоновые дисперсии (config/thresholds.yaml). Это «затравка» калибровки и удобный
    набор по умолчанию — НЕ предел открытия. (Бывшее значение context.CORE, 1:1.)

  • ТРИ — SEALABLE_UNIVERSE. §9-разрешимый универсум: инструменты, по которым У НАС ЕСТЬ источник
    цены с достаточной историей, чтобы ЗАПЕЧАТАТЬ форвард-прогноз (П16). ДИНАМИЧЕСКИЙ — растёт по
    мере добора истории на лету (Этап 4), не ограничен seed-ом. Индексы (.INDX) исключены: это
    референсы бенчмарка, а не торгуемые инструменты (§14). Узел каскада без разрешимого инструмента
    → лист ожидания §17 / research-only (паттерн SPCX), а не в мусор.

Этап 0 НЕ меняет поведение: context.CORE = CALIBRATION_SEED (тот же список), а sealable_*/discovery_*
пока никто в боевом пути не вызывает — это каркас под Этапы 1/4. Модуль самодостаточен (не импортирует
context), чтобы context мог импортировать его без цикла.
"""
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "oracle.db"

# Курируемая затравка калибровки (1:1 бывший context.CORE; SPCX некалибруем — 2 бара, research-only).
# Менять состав — решение пользователя (§14/§30). Это НЕ предел открытия (см. discovery_is_open()).
CALIBRATION_SEED = [
    "BNO.US", "USO.US", "SPY.US", "DBC.US", "CPER.US", "COPX.US",
    "SPCX.US", "RKLB.US", "ASTS.US",
    "VRT.US", "GEV.US", "ETN.US", "CLF.US", "NUE.US",  # цепочка ai_power (пилот тектоники)
]

# §9-разрешимость: минимум баров истории, чтобы посчитать волу/порог и запечатать прогноз.
# Совпадает с context.MIN_THEME_HISTORY_BARS (§6/§23).
MIN_SEALABLE_BARS = 20

# Маркер канона: открытие не ограничено списком (§6 Эт.1, §17.2). Здесь — единая точка правды.
DISCOVERY_OPEN = True


def calibration_seed():
    """Курируемая затравка калибровки (НЕ предел открытия). Копия — чтобы вызывающий не мутировал."""
    return list(CALIBRATION_SEED)


def discovery_is_open():
    """True: пространство открытия (скан событий/каскадов) НЕ ограничено тикер-списком (§6/§17.2).
    Существует как явная точка правды против повторного скатывания к закрытому универсуму."""
    return DISCOVERY_OPEN


def _is_index(symbol):
    """Индекс-референс (.INDX) — не торгуемый инструмент (§14), запечатывать на него нельзя."""
    return bool(symbol) and symbol.upper().endswith(".INDX")


def _connect(db=None):
    db = pathlib.Path(db) if db else DB
    if not db.exists():
        return None
    return sqlite3.connect(str(db))


def _bar_counts(con):
    try:
        rows = con.execute("SELECT symbol, COUNT(*) FROM quotes GROUP BY symbol").fetchall()
    except sqlite3.OperationalError:
        return None
    return {r[0]: r[1] for r in rows if r[0]}


def sealable_universe(con=None, db=None, min_bars=MIN_SEALABLE_BARS):
    """§9-разрешимый универсум: торгуемые инструменты с источником цены и ≥min_bars истории.

    ДИНАМИЧЕСКИЙ — отражает содержимое quotes, растёт при доборе истории (Этап 4). Индексы (.INDX)
    исключены (§14). Если БД/таблица недоступны — безопасный фолбэк на CALIBRATION_SEED (минус .INDX).
    """
    own = con is None
    if con is None:
        con = _connect(db)
    if con is None:
        return [s for s in CALIBRATION_SEED if not _is_index(s)]
    try:
        counts = _bar_counts(con)
        if counts is None:
            return [s for s in CALIBRATION_SEED if not _is_index(s)]
        return sorted(s for s, n in counts.items() if n >= min_bars and not _is_index(s))
    finally:
        if own:
            con.close()


def is_sealable(symbol, con=None, db=None, min_bars=MIN_SEALABLE_BARS):
    """Можно ли ЗАПЕЧАТАТЬ форвард-прогноз на этот инструмент (§9/П16)?

    Проверяет ТОЛЬКО наличие источника цены с достаточной историей — не калибровку и не качество
    идеи. Индекс (.INDX) → False (§14). Нет БД → фолбэк: символ из seed и не индекс.
    """
    if not symbol or _is_index(symbol):
        return False
    own = con is None
    if con is None:
        con = _connect(db)
    if con is None:
        return symbol in CALIBRATION_SEED
    try:
        n = con.execute("SELECT COUNT(*) FROM quotes WHERE symbol=?", (symbol,)).fetchone()[0]
        return n >= min_bars
    finally:
        if own:
            con.close()
