# -*- coding: utf-8 -*-
"""Полка длинных тем (автопилот 15.11). Герметично: все источники — стабы, сети нет."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import long_shelf as LS     # noqa: E402

TODAY = datetime.date(2026, 11, 15)
THEMES = {"правила": {"просадка_входа_проц": 20},
          "темы": [{"id": "t", "название": "спрос на вычисления", "тезис": "гипотеза", "бумаги": [
              {"бумага": "AAA.US", "имя": "Альфа", "роль": "делает станки"},
              {"бумага": "BBB.US", "имя": "Бета", "роль": "делает чипы"}]}]}


def _closes(drop):
    rows = [((TODAY - datetime.timedelta(days=60 - i)).isoformat(), 100.0) for i in range(60)]
    rows[-1] = (TODAY.isoformat(), 100.0 * (1 + drop))
    return rows


def _fetch(drops, revs, vals=None, fail=()):
    def closes(sym, today):
        if "closes" in fail:
            raise OSError("сеть")
        return _closes(drops[sym])
    return {"closes": closes, "revision": lambda s, t: revs.get(s),
            "valuation": lambda s, t: (vals or {}).get(s) or {"причина": "нет"},
            "earnings": lambda syms, t: {"AAA.US": "2026-11-20"}}


def test_statuses_and_text(tmp_path):
    r = LS.tick(TODAY, push=True, out_dir=tmp_path / "sh", notices_path=tmp_path / "n.jsonl", themes=THEMES,
                fetch=_fetch({"AAA.US": -0.30, "BBB.US": -0.05}, {"AAA.US": 0.04, "BBB.US": -0.15}))
    st = {b["бумага"]: b["статус"] for b in r["снимок"]["темы"][0]["бумаги"]}
    assert st == {"AAA.US": "зона входа", "BBB.US": "тезис под вопросом"}
    assert not r["линт"] and r["отправлено"]
    t = json.loads((tmp_path / "n.jsonl").read_text("utf-8").splitlines()[0])["text"]
    assert "Альфа (AAA.US) — СЕЙЧАС В ЗОНЕ ВХОДА: -30% от максимума за год" in t and "прогноз прибыли срезали на 15%" in t
    assert "Альфа — 22 ноября" in t                                         # проверка тезиса после отчёта
    assert "сразу 1 из 2 бумаг" in t                                       # половина полки — оговорка про сектор
    assert (tmp_path / "sh" / "latest.json").exists() and (tmp_path / "sh" / "2026-11-15.json").exists()


def test_no_push_without_flag_and_week_change(tmp_path):
    kw = dict(out_dir=tmp_path / "sh", notices_path=tmp_path / "n.jsonl", themes=THEMES)
    LS.tick(TODAY - datetime.timedelta(days=7), push=False,
            fetch=_fetch({"AAA.US": -0.05, "BBB.US": -0.05}, {}), **kw)
    r = LS.tick(TODAY, push=False, fetch=_fetch({"AAA.US": -0.30, "BBB.US": -0.05}, {}), **kw)
    assert not r["отправлено"] and not (tmp_path / "n.jsonl").exists()
    assert "Альфа — было «ждать», стало «зона входа»" in r["текст"]
    assert "Бета (BBB.US) — ЖДАТЬ" in r["текст"] and "зона входа начнётся при цене ниже $80.00 (сейчас $95.00)" in r["текст"]


def test_source_failure_is_honest(tmp_path):
    r = LS.tick(TODAY, push=False, out_dir=tmp_path / "sh", themes=THEMES,
                fetch=_fetch({}, {}, fail=("closes",)))
    b = r["снимок"]["темы"][0]["бумаги"][0]
    assert b["статус"] == "ждать" and b["почему"] == ["нет данных о годовом максимуме"] and b["сбои"]


def test_real_themes_load():
    d = LS.load_themes()
    syms = [b["бумага"] for t in d["темы"] for b in t["бумаги"]]
    assert len(d["темы"]) >= 5 and "ASML.US" in syms
    assert all(b.get("источник", "").startswith("https://") for t in d["темы"] for b in t["бумаги"])
