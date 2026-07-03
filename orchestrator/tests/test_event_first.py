# -*- coding: utf-8 -*-
"""Смоук event-first контура end-to-end в mock (orchestrator/event_first.py, Этап 6).

Доказывает сшивку: открытый скан → источники → полный контур (mock агенты+суд) + каскад-резолв.
Пропускается без storage/oracle.db. mock не ходит в сеть и не пишет в costs (бюджет §30).
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import sqlite3                                # noqa: E402

from orchestrator import event_first as EF    # noqa: E402
from mathlib import cascade as CAS            # noqa: E402
from mathlib import sealing as SEAL           # noqa: E402


def test_window_return_asof_gate_blocks_future_bars():
    # F3#25 (§5.3): asof-гейт — бары ПОСЛЕ даты решения НЕ должны влиять на шок (защита П16).
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, adjusted_close REAL)")
    # 9 баров: цена ровно 100 до и включая asof, затем СКАЧОК вверх после asof
    days = [f"2026-06-{d:02d}" for d in range(1, 10)]        # 01..09
    px = [100.0] * 6 + [200.0, 200.0, 200.0]                 # скачок с 07-го
    con.executemany("INSERT INTO quotes VALUES ('X.US',?,?,?)",
                    [(d, p, p) for d, p in zip(days, px)])
    con.commit()
    # asof = 06-е: окно видит только плоские 100 → шок 0; будущий скачок отрезан
    gated = EF._window_return(con, "X.US", asof="2026-06-06")
    assert gated == 0.0
    # без asof: последние 6 баров включают скачок → шок ≠ 0 (доказывает, что гейт реально режет)
    ungated = EF._window_return(con, "X.US")
    assert ungated is not None and ungated != 0.0
    con.close()


@pytest.mark.skipif(not EF.DB.exists(), reason="нет storage/oracle.db")
def test_event_first_mock_wiring():
    p = EF.run_event_first(mode="mock", k=2, write=False)
    assert p["mode"] == "mock"
    assert "скан" in p and "по_источникам" in p
    # каждая запечатываемая каскад-спека §9-разрешима
    for s in p["по_источникам"]:
        cr = s.get("каскад_резолв")
        if cr:
            for sp in cr["запечатываемо"]:
                assert SEAL.is_resolvable(sp["prediction"])
