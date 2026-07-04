# -*- coding: utf-8 -*-
"""mathlib/calibration/loader.py — загрузка дневных рядов из storage/oracle.db.

ЧЕСТНАЯ ЗОНА §23.1: детерминированный код «не помнит будущее», поэтому полный
walk-forward на истории легален. Этот модуль только читает котировки и выравнивает
их по датам — никакой сети, никаких LLM. Источник — таблица quotes (data/eodhd.py).
"""
import sqlite3
import pathlib
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
DB = ROOT / "storage" / "oracle.db"


class Series:
    """Дневной ряд одного инструмента, отсортированный по дате (возрастание).

    Поля — numpy-массивы одинаковой длины: dates(str), open, high, low, close,
    adj (adjusted_close с фолбеком на close), volume(float).
    """

    __slots__ = ("symbol", "dates", "open", "high", "low", "close", "adj", "volume")

    def __init__(self, symbol, rows):
        self.symbol = symbol
        self.dates = np.array([r[0] for r in rows])
        self.open = np.array([_f(r[1]) for r in rows], dtype=float)
        self.high = np.array([_f(r[2]) for r in rows], dtype=float)
        self.low = np.array([_f(r[3]) for r in rows], dtype=float)
        self.close = np.array([_f(r[4]) for r in rows], dtype=float)
        self.adj = np.array([_f(r[5] if r[5] is not None else r[4]) for r in rows], dtype=float)
        self.volume = np.array([_f(r[6]) for r in rows], dtype=float)

    def __len__(self):
        return self.dates.size

    def __repr__(self):
        rng = f"{self.dates[0]}..{self.dates[-1]}" if len(self) else "empty"
        return f"Series({self.symbol}, n={len(self)}, {rng})"


def _f(x):
    return float("nan") if x is None else float(x)


def connect(db=DB):
    return sqlite3.connect(str(db))


def load_series(symbol, db=DB):
    """Загрузить один инструмент как Series (по возрастанию даты)."""
    con = connect(db)
    try:
        rows = con.execute(
            "SELECT date, open, high, low, close, adjusted_close, volume "
            "FROM quotes WHERE symbol=? ORDER BY date ASC",
            (symbol,),
        ).fetchall()
    finally:
        con.close()
    return Series(symbol, rows)


def adjusted_view(series):
    """Вернуть копию Series, где цены переведены на adjusted_close (сплиты/дивиденды).

    close ← adj; open/high/low масштабируются на тот же фактор f=adj/close (OHLC остаются
    согласованными для ATR/пробоев). volume не трогаем (raw). Для инструментов, где
    adjusted_close==close (нет данных корпдействий), результат идентичен исходному.
    Использовать ВЕЗДЕ, где считаются доходности/движения; в costs (долларовый оборот)
    нужен RAW close*volume — там adjusted_view НЕ применять.
    """
    c = series.close
    a = series.adj
    f = np.ones_like(c)
    ok = np.isfinite(c) & (c > 0) & np.isfinite(a)
    f[ok] = a[ok] / c[ok]
    new = Series.__new__(Series)
    new.symbol = series.symbol
    new.dates = series.dates
    new.close = a.copy()
    new.adj = a.copy()
    new.open = series.open * f
    new.high = series.high * f
    new.low = series.low * f
    new.volume = series.volume
    return new


def list_symbols(db=DB):
    con = connect(db)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM quotes ORDER BY symbol").fetchall()]
    finally:
        con.close()


def load_aligned(symbols, db=DB, asof=None):
    """Загрузить несколько инструментов, выровненных по ОБЩИМ датам (пересечение).

    Возвращает (dates, {symbol: Series}), где у каждого Series одинаковый набор дат.
    Нужен для измерения lead-lag причинных связей (§23.1 п.2) и кросс-активных
    предвестников (§23.1 п.3) на синхронных рядах.
    asof='YYYY-MM-DD' (replay, ночь 04.07): только даты <= asof — чувствительность
    «как была бы на дату», без look-ahead (П16)."""
    raw = {s: load_series(s, db) for s in symbols}
    common = None
    for s, ser in raw.items():
        ds = {d for d in ser.dates.tolist() if (asof is None or d <= asof)}
        common = ds if common is None else (common & ds)
    common = np.array(sorted(common)) if common else np.array([])
    out = {}
    for s, ser in raw.items():
        idx = {d: i for i, d in enumerate(ser.dates.tolist())}
        sel = np.array([idx[d] for d in common.tolist()], dtype=int)
        new = Series.__new__(Series)
        new.symbol = s
        new.dates = common
        for attr in ("open", "high", "low", "close", "adj", "volume"):
            setattr(new, attr, getattr(ser, attr)[sel] if sel.size else np.array([]))
        out[s] = new
    return common, out
