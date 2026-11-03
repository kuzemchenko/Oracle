# -*- coding: utf-8 -*-
"""Снимок серверных закрытий для облачного охотника (ops/price_snapshot.py, аудит 03.11)."""
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import price_snapshot as PS   # noqa: E402


def test_build_write_lookup(tmp_path):
    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, adjusted_close REAL)")
    d0 = datetime.date(2026, 8, 1)
    for i in range(90):
        d = (d0 + datetime.timedelta(days=i)).isoformat()
        con.execute("INSERT INTO quotes VALUES (?,?,?,?)", ("MP.US", d, 40 + (i % 3), 1.0))
    con.execute("INSERT INTO quotes VALUES ('DEAD.US','2026-01-05',10,10)")     # мёртвая — не берём
    con.commit()
    con.close()
    out = tmp_path / "snap.json"
    snap = PS.write(out, db_path=db, today=datetime.date(2026, 10, 30))
    assert list(snap["symbols"]) == ["MP.US"] and snap["as_of"] == "2026-10-29"
    assert snap["symbols"]["MP.US"][-1] == ["2026-10-29", 42.0]                  # сырой close, не adjusted
    r = PS.lookup("MP", "2026-09-23", path=out)
    assert r["on_or_before"] == {"date": "2026-09-23", "close": 42.0} and r["last_close"] == 42.0
    assert r["sigma_daily"] and r["sigma_daily"] > 0
    assert PS.lookup("MP.US", "2026-07-01", path=out)["on_or_before"] is None      # раньше истории — честно нет
    assert "ошибка" in PS.lookup("ZZZ.US", path=out)
    assert "ошибка" in PS.lookup("MP.US", path=tmp_path / "none.json")
    assert json.loads(out.read_text("utf-8"))["source"].startswith("EODHD close")
