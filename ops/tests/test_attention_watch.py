# -*- coding: utf-8 -*-
"""Q-тренды (09.11): тикер → страница, ежедневный обход, форвард-счёт по записанным заранее сигналам."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops import attention_watch as AW   # noqa: E402


def _views(page, series):
    end = datetime.date(2026, 11, 8)
    n = len(series)
    items = [{"timestamp": (end - datetime.timedelta(days=n - 1 - i)).strftime("%Y%m%d") + "00", "views": v}
             for i, v in enumerate(series)]
    return {"items": items}


def fake_get(url):
    if "fundamentals/FSLR" in url:
        return {"Name": "First Solar, Inc.", "Type": "Common Stock"}
    if "fundamentals/SPY" in url:
        return {"Name": "SPDR S&P 500", "Type": "ETF"}
    if "fundamentals/NVDA" in url:
        return {"Name": "NVIDIA Corporation", "Type": "Common Stock"}
    if "/First_Solar/" in url:
        return _views("First_Solar", [100] * 56 + [300, 350, 400, 420, 450, 480, 500])
    if "/Nvidia/" in url:
        return _views("Nvidia", [5000] * 63)
    raise OSError("404")


def test_resolve_pages(tmp_path):
    p = tmp_path / "pages.json"
    res = AW.resolve(["FSLR.US", "SPY.US", "NVDA.US"], pages_path=p, get=fake_get, pause=0)
    assert res["FSLR.US"]["page"] == "First_Solar" and res["SPY.US"]["page"] is None
    assert res["NVDA.US"]["page"] == "Nvidia"                   # «NVIDIA» → «Nvidia»
    assert json.loads(p.read_text("utf-8"))["FSLR.US"]["проверено"]


def test_scan_and_forward(tmp_path):
    pages = tmp_path / "pages.json"
    pages.write_text(json.dumps({"FSLR.US": {"page": "First_Solar"}, "NVDA.US": {"page": "Nvidia"}}), encoding="utf-8")
    rep = AW.scan(pages_path=pages, out_dir=tmp_path / "att", get=fake_get, today="2026-11-09", pause=0)
    assert rep["измерено"] == 2 and [s["бумага"] for s in rep["сигналы"]] == ["FSLR.US"]
    assert rep["сигналы"][0]["фаза"] == "разгон"
    days = [f"2026-11-{d:02d}" for d in range(2, 30)]
    snap = {"symbols": {"FSLR.US": [[d, 100 + i] for i, d in enumerate(days)],
                        "SPY.US": [[d, 100.0] for d in days]}}
    cp = tmp_path / "closes.json"
    cp.write_text(json.dumps(snap), encoding="utf-8")
    fw = AW.forward(out_dir=tmp_path / "att", closes_path=cp, horizons=(5,))
    row = fw["по_фазам"]["разгон|5д"]
    assert fw["сигналов"] == 1 and row["n"] == 1 and row["выше_рынка"] == 1 and row["средний_ход_сверх_рынка"] > 0
    assert "мало наблюдений" in fw["оговорка"]
