# -*- coding: utf-8 -*-
"""Q-тренды-быстро (09.11): новости GDELT по 15 мин, Википедия по часам, темп объёма торгов — расчёт и сигналы."""
import datetime
import io
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops import attention_fast as F      # noqa: E402
from mathlib import attention_trend as AT   # noqa: E402

WL = {"NVDA.US": {"page": "Nvidia", "org": "nvidia"}, "FSLR.US": {"page": "First_Solar", "org": "first solar"}}


def _gkg(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", "\n".join("\t".join(["id"] + [""] * 12 + [orgs] + [""] * 13) for orgs in rows))
    return buf.getvalue()


def test_gkg_counts_once_per_article():
    raw = _gkg(["nvidia,10;nvidia,40;google,5", "first solar,3", "apple,1"])
    assert F.gkg_counts(raw, {t: w["org"] for t, w in WL.items()}) == (3, {"NVDA.US": 1, "FSLR.US": 1})


def test_surge_and_volume_pace_math():
    assert AT.surge(30, [3, 4, 2, 5, 3, 4])["всплеск"] is True
    assert AT.surge(2, [3, 4, 2, 5, 3, 4])["всплеск"] is False
    assert not AT.surge(30, [1, 2])["ok"]
    vp = AT.volume_pace(3_000_000, 1_000_000, 195)          # полсессии, объём ×3 от дня → темп ×6
    assert vp["всплеск"] and vp["темп"] == 6.0
    assert not AT.volume_pace(1, 1_000_000, 10)["ok"]


def test_session_minutes_dst():
    assert F.session_minutes(datetime.datetime(2026, 11, 9, 14, 30, tzinfo=datetime.timezone.utc)) == 0    # зима: 9:30 ET
    assert F.session_minutes(datetime.datetime(2026, 7, 9, 13, 30, tzinfo=datetime.timezone.utc)) == 0     # лето
    assert F.session_minutes(datetime.datetime(2026, 11, 8, 15, 0, tzinfo=datetime.timezone.utc)) is None  # воскресенье


def test_signals_from_collected_files(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "FAST", tmp_path / "fast")
    monkeypatch.setattr(F, "LATEST", tmp_path / "fast_latest.json")
    monkeypatch.setattr(F, "SIGNALS_LOG", tmp_path / "fast_signals.jsonl")
    now = datetime.datetime(2026, 11, 9, 19, 0, tzinfo=datetime.timezone.utc)
    start = now - datetime.timedelta(days=3)
    t = start
    while t <= now:
        hot = t > now - datetime.timedelta(hours=2)
        F._append(F.FAST / f"news_{t.date().isoformat()}.jsonl",
                  {"stamp": t.strftime("%Y%m%d%H%M%S"), "статей": 1000, "упоминания": {"FSLR.US": 6 if hot else 1}})
        t += datetime.timedelta(minutes=15)
    F._append(F.FAST / "volume_2026-11-09.jsonl", {"ts": "2026-11-09T19:00", "минут_сессии": 270,
                                                   "объём": {"NVDA.US": 900_000_000, "FSLR.US": 1_000_000}})
    rep = F.signals(WL, now, adv_map={"NVDA.US": 200_000_000, "FSLR.US": 3_000_000},
                    options_fn=lambda t: {"ok": True, "кратно": 2.5})
    got = {s["бумага"]: s["следы"] for s in rep["сигналы"]}
    assert "новости" in got["FSLR.US"] and "объём_торгов" in got["NVDA.US"] and "опционы" in got["NVDA.US"]
    F.signals(WL, now, adv_map={"NVDA.US": 200_000_000}, options_fn=lambda t: {"ok": False})
    log = [json.loads(x) for x in (tmp_path / "fast_signals.jsonl").read_text("utf-8").splitlines()]
    assert len(log) == len({(r["бумага"], r["след"]) for r in log})          # один сигнал на бумагу и след в день
