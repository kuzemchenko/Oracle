# -*- coding: utf-8 -*-
"""Q-в-цене (09.11): P/E и P/S против собственной истории — без заглядывания вперёд, убыток не считается."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import multiple_history as MH   # noqa: E402

QDATES = ["2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31", "2023-03-31", "2023-06-30", "2023-09-30",
          "2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30"]


def _rows(vals, field):
    return {d: {"date": d, "filing_date": d[:8] + "28" if d[5:7] != "12" else d[:4] + "-12-31", field: v}
            for d, v in zip(QDATES, vals)}


def test_ttm_and_asof_filing():
    q = MH.quarterly(_rows([1] * 14, "netIncome"), "netIncome")
    t = MH.ttm(q)
    assert len(t) == 11 and t[0][2] == 4
    assert MH._asof(t, "2022-12-30") is None          # подан 31.12 — на 30.12 ещё неизвестен


def test_history_percentile_and_loss_refusal():
    inc = _rows([10] * 14, "totalRevenue")
    sh = _rows([100] * 14, "commonStockSharesOutstanding")
    dates = [f"{y}-{m:02d}-28" for y in (2022, 2023, 2024, 2025) for m in range(1, 13)]
    closes = [1.0 + i * 0.05 for i in range(len(dates))]          # цена растёт → мультипликатор растёт
    res = MH.history(dates, closes, MH.ttm(MH.quarterly(inc, "totalRevenue")),
                     MH.quarterly(sh, "commonStockSharesOutstanding"), years=5, today="2025-12-28")
    assert res["ok"] and res["перцентиль"] == 1.0 and res["сейчас"] > res["медиана"]
    assert "выше, чем в 100%" in MH.words("P/S", res)
    loss = MH.history(dates, closes, MH.ttm(MH.quarterly(_rows([-1] * 14, "netIncome"), "netIncome")),
                      MH.quarterly(sh, "commonStockSharesOutstanding"), today="2025-12-28")
    assert not loss["ok"] and "убыток" in loss["причина"]
    assert not MH.history([], [], [], [])["ok"]
