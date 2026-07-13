# -*- coding: utf-8 -*-
"""Тесты Д1 для event_scan (df per-instrument из fdr.tail_df) и context._news(asof).

Ключевая гарантия: БЕЗ секции tail_df поведение скана байт-в-байт прежнее (константы F2#19,
никаких новых полей в протоколе)."""
import json
import sqlite3
import pathlib
import sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_scan as ES     # noqa: E402
from orchestrator import context as C         # noqa: E402
from mathlib import tailprob as TP            # noqa: E402

IND = {
    "AAA.US": {"ret_z_20": 3.1, "vol_z_log_20": 2.0},
    "BBB.US": {"ret_z_20": -2.5, "vol_z_20": 4.0},   # лог-объёма нет → сырой фолбэк
}
TAIL = {"fallback": {"ret_z_20": 30.0, "vol_z_log_20": 20.0, "vol_z_20": 3, "note": "x"},
        "per_instrument": {"AAA.US": {"ret_z_20": 15.0}}}


# ── без секции: байт-в-байт прежнее поведение ───────────────────────────────────────

def test_no_section_byte_identical_constants():
    out = ES.scan_events(indicators=IND)
    sigs = {(s["символ"], s["метрика"]): s for s in out["сигналы"] if s["вид"] == "price"}
    aaa = sigs[("AAA.US", "ret_z_20")]
    assert aaa["df_нуля"] == 5                              # константа F2#19
    assert aaa["p_value"] == round(TP.student_t_two_sided_p(3.1, 5), 4)
    assert sigs[("AAA.US", "vol_z_log_20")]["df_нуля"] == 6
    assert sigs[("BBB.US", "vol_z_20")]["df_нуля"] == 3     # сырой фолбэк
    for s in out["сигналы"]:
        assert "df_источник" not in s                       # новых полей нет
    assert "tail_df_протокол" not in out
    # и дословно тот же протокол, что второй вызов без tail_df (детерминизм)
    assert json.dumps(out, sort_keys=True, default=str) == \
        json.dumps(ES.scan_events(indicators=IND, tail_df=None), sort_keys=True, default=str)


# ── с секцией: per-instrument → фолбэк калибровки → константа ───────────────────────

def test_with_section_per_instrument_and_fallbacks():
    out = ES.scan_events(indicators=IND, tail_df=TAIL)
    sigs = {(s["символ"], s["метрика"]): s for s in out["сигналы"] if s["вид"] == "price"}
    aaa_ret = sigs[("AAA.US", "ret_z_20")]
    assert aaa_ret["df_нуля"] == 15.0 and aaa_ret["df_источник"] == "per_instrument"
    assert aaa_ret["p_value"] == round(TP.student_t_two_sided_p(3.1, 15.0), 4)
    aaa_vol = sigs[("AAA.US", "vol_z_log_20")]
    assert aaa_vol["df_нуля"] == 20.0 and aaa_vol["df_источник"] == "фолбэк_калибровки"
    bbb_ret = sigs[("BBB.US", "ret_z_20")]
    assert bbb_ret["df_нуля"] == 30.0 and bbb_ret["df_источник"] == "фолбэк_калибровки"
    bbb_vol = sigs[("BBB.US", "vol_z_20")]                  # сырой объём — из фолбэка секции
    assert bbb_vol["df_нуля"] == 3 and bbb_vol["df_источник"] == "фолбэк_калибровки"
    # протокол скана декларирует источники df (П8)
    proto = out["tail_df_протокол"]
    assert proto["df_по_источникам"]["per_instrument"] == 1
    assert "note" not in proto["фолбэк"]


def test_with_section_missing_everything_falls_to_constants():
    out = ES.scan_events(indicators=IND, tail_df={"per_instrument": {}})
    sigs = {(s["символ"], s["метрика"]): s for s in out["сигналы"] if s["вид"] == "price"}
    s = sigs[("AAA.US", "ret_z_20")]
    assert s["df_нуля"] == 5 and s["df_источник"] == "константа_F2#19"


def test_resolve_df_ignores_garbage_values():
    td = {"per_instrument": {"AAA.US": {"ret_z_20": "мусор"}}, "fallback": {"ret_z_20": -1}}
    df, src = ES._resolve_df(td, "AAA.US", "ret_z_20", 5)
    assert df == 5 and src == "константа_F2#19"


def test_tail_df_from_thresholds_parsing():
    assert ES.tail_df_from_thresholds({"fdr": {"tail_df": {"fallback": {}}}}) == {"fallback": {}}
    assert ES.tail_df_from_thresholds({"fdr": {}}) is None
    assert ES.tail_df_from_thresholds({"fdr": {"tail_df": "битая строка"}}) is None
    assert ES.tail_df_from_thresholds({}) is None


def test_live_config_has_tail_df_section():
    """Регрессия: боевой config/thresholds.yaml после Д1 реально даёт секцию скану."""
    pytest.skip("Д1 деактивирован в боевом config/thresholds.yaml до прохождения гейта se-d1 "
                "(откат 13.07). Снять skip после гейта Д1 + перегенерации конфига драйвером.")
    td = ES.tail_df_from_thresholds()
    assert td and td.get("per_instrument"), "fdr.tail_df не читается из config/thresholds.yaml"


# ── context._news(asof): срез «как было бы» ─────────────────────────────────────────

def _news_db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE news (id TEXT PRIMARY KEY, source TEXT, title TEXT, lang TEXT, "
                "published_at TEXT, fetched_at TEXT, dup_of TEXT)")
    rows = [
        ("1", "s", "старая", "en", "2026-07-01T08:00:00+00:00", "2026-07-01T09:00:00+00:00", None),
        ("2", "s", "видна на срезе", "en", "2026-07-02T08:00:00+00:00", "2026-07-02T08:30:00+00:00", None),
        ("3", "s", "зафетчена ПОЗЖЕ среза", "en", "2026-07-02T08:00:00+00:00", "2026-07-03T09:00:00+00:00", None),
        ("4", "s", "опубликована позже", "en", "2026-07-04T10:00:00+00:00", "2026-07-04T11:00:00+00:00", None),
        ("5", "s", "дубль", "en", "2026-07-02T07:00:00+00:00", "2026-07-02T07:30:00+00:00", "2"),
    ]
    con.executemany("INSERT INTO news VALUES (?,?,?,?,?,?,?)", rows)
    return con


def test_news_asof_slices_by_fetched_and_published():
    con = _news_db()
    items = C._news(con, limit=10, asof="2026-07-02T09:00:00+00:00")
    titles = [it["title"] for it in items]
    assert titles == ["видна на срезе", "старая"]   # №3 (поздний фетч) и №4 (позже) исключены, №5 дубль
    con.close()


def test_news_asof_none_is_live_behavior():
    con = _news_db()
    items = C._news(con, limit=10)
    titles = [it["title"] for it in items]
    assert titles == ["опубликована позже", "видна на срезе", "зафетчена ПОЗЖЕ среза", "старая"]
    con.close()
