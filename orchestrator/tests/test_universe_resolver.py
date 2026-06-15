# -*- coding: utf-8 -*-
"""Тесты брандмауэра «открытие vs запечатывание» (orchestrator/universe_resolver.py, Этап 0).

Проверяет:
  • РЕГРЕССИЯ Этапа 0: значение context.CORE не изменилось (тот же список 14) → поведение то же;
  • CORE — это и есть CALIBRATION_SEED (единый источник правды, не два хардкода);
  • discovery_is_open() = True (открытие не ограничено списком тикеров, §6/§17.2);
  • sealable_universe()/is_sealable() ДИНАМИЧЕСКИ читают quotes: ≥min_bars, индексы (.INDX) вон,
    инструмент с малой историей (как SPCX, 2 бара) — НЕ запечатываем;
  • фолбэк без БД — seed минус индексы.
"""
import sys
import sqlite3
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import universe_resolver as U   # noqa: E402
from orchestrator import context as C             # noqa: E402

# Историческое значение CORE до Этапа 0 — регрессионный якорь «поведение не изменилось».
_LEGACY_CORE = ["BNO.US", "USO.US", "SPY.US", "DBC.US", "CPER.US", "COPX.US",
                "SPCX.US", "RKLB.US", "ASTS.US",
                "VRT.US", "GEV.US", "ETN.US", "CLF.US", "NUE.US"]


def test_core_value_unchanged():
    """Этап 0 не меняет состав: context.CORE строго равен прежним 14 тикерам."""
    assert list(C.CORE) == _LEGACY_CORE


def test_core_is_calibration_seed():
    """Единый источник правды: context.CORE и есть CALIBRATION_SEED (нет второго хардкода)."""
    assert list(C.CORE) == U.calibration_seed()
    assert U.calibration_seed() == _LEGACY_CORE


def test_calibration_seed_returns_copy():
    """Аксессор отдаёт копию — вызывающий не может молча мутировать затравку."""
    seed = U.calibration_seed()
    seed.append("XXX.US")
    assert "XXX.US" not in U.calibration_seed()


def test_discovery_is_open():
    """Открытие не ограничено списком тикеров (§6 Эт.1 / §17.2)."""
    assert U.discovery_is_open() is True


def _make_db(tmp_path):
    db = tmp_path / "q.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT)")
    rows = []
    rows += [("BNO.US", f"d{i}") for i in range(40)]      # есть, торгуемо → разрешимо
    rows += [("NEW.US", f"d{i}") for i in range(25)]      # НЕ из seed, но истории хватает → разрешимо (динамика!)
    rows += [("SPCX.US", "d0"), ("SPCX.US", "d1")]        # 2 бара — мало → НЕ запечатываем
    rows += [("BCOM.INDX", f"d{i}") for i in range(99)]   # индекс — не торгуем (§14) → вон
    con.executemany("INSERT INTO quotes VALUES (?,?)", rows)
    con.commit()
    con.close()
    return db


def test_sealable_universe_dynamic(tmp_path):
    """§9-реестр читает quotes динамически: ≥min_bars, индексы и короткую историю отсекает,
    а НОВЫЙ тикер вне seed — включает (доказательство, что универсум не заморожен)."""
    db = _make_db(tmp_path)
    su = U.sealable_universe(db=db)
    assert "BNO.US" in su
    assert "NEW.US" in su          # динамика: не из seed, но разрешим
    assert "SPCX.US" not in su     # мало истории
    assert "BCOM.INDX" not in su   # индекс не торгуем


def test_is_sealable_rules(tmp_path):
    db = _make_db(tmp_path)
    assert U.is_sealable("BNO.US", db=db) is True
    assert U.is_sealable("NEW.US", db=db) is True
    assert U.is_sealable("SPCX.US", db=db) is False     # 2 бара < порога
    assert U.is_sealable("BCOM.INDX", db=db) is False   # индекс (§14)
    assert U.is_sealable("ZZZ.US", db=db) is False      # нет источника цены вовсе
    assert U.is_sealable("", db=db) is False
    assert U.is_sealable(None, db=db) is False


def test_fallback_without_db(tmp_path):
    """Нет БД → безопасный фолбэк на seed минус индексы (никаких исключений)."""
    missing = tmp_path / "nope.db"
    su = U.sealable_universe(db=missing)
    assert su == [s for s in _LEGACY_CORE if not s.endswith(".INDX")]
    assert U.is_sealable("BNO.US", db=missing) is True   # из seed
    assert U.is_sealable("NEW.US", db=missing) is False  # не из seed, БД нет
