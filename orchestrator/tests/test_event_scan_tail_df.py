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


def test_resolve_df_ignores_bool(monkeypatch):
    """Д1 #9: bool — подкласс int (True==1); df=1.0 из ошибочного True в конфиге отравил бы
    t-хвост. _resolve_df обязан игнорировать bool и на per_instrument, и на фолбэке."""
    td = {"per_instrument": {"AAA.US": {"ret_z_20": True}}, "fallback": {"ret_z_20": True}}
    df, src = ES._resolve_df(td, "AAA.US", "ret_z_20", 5)
    assert df == 5 and src == "константа_F2#19"
    # валидное число рядом по-прежнему берётся
    td2 = {"per_instrument": {"AAA.US": {"ret_z_20": 12.0}}}
    assert ES._resolve_df(td2, "AAA.US", "ret_z_20", 5) == (12.0, "per_instrument")


def test_bh_runs_on_unrounded_pvalues(monkeypatch):
    """Д1 #5: Benjamini–Hochberg обязан получать p ПОЛНОЙ точности, а не округлённые до 4 знаков
    (иначе округление меняет набор открытий FDR). Протокол при этом показывает округлённый p."""
    captured = {}

    def fake_bh(pvals, q):
        captured["pvals"] = list(pvals)
        return {"rejected": [False] * len(pvals), "qvalues": list(pvals), "n_signif": 0}

    monkeypatch.setattr(ES.fdr, "benjamini_hochberg", fake_bh)
    # z, дающий p с более чем 4 значащими знаками после запятой
    out = ES.scan_events(indicators={"X.US": {"ret_z_20": 3.333}})
    p_raw = captured["pvals"][0]
    assert p_raw != round(p_raw, 4), "в BH ушёл уже округлённый p"
    assert out["сигналы"][0]["p_value"] == round(p_raw, 4)   # показ округлён
    assert "_p_raw" not in out["сигналы"][0]                  # внутреннее поле не утекло


def test_bh_rounding_would_flip_decision():
    """Прямой контрпример из ревью: истинный p чуть выше порога BH проходит ложно, если его
    округлить. m=2, q=0.1 → порог ранга-1 = 0.05; p=0.05004 (raw) НЕ проходит, round→0.05 прошёл бы.
    Собираем статистические сигналы вручную и прогоняем через настоящий BH обоими путями."""
    from mathlib import fdr as FDR
    p_raw = 0.05004
    # BH по сырому p: с ним сигнал НЕ отвергается (порог 0.05); по округлённому — отверг бы
    assert FDR.benjamini_hochberg([p_raw, 0.9], q=0.1)["rejected"][0] is False
    assert FDR.benjamini_hochberg([round(p_raw, 4), 0.9], q=0.1)["rejected"][0] is True


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


# ── Д1 #8: боевой гард артефактов фида (volume=0) и гейт давности бара ───────────────

def _bars(n, last_vol=1000):
    """n синтетических баров с лёгкой динамикой; объём последнего бара = last_vol."""
    out = []
    for i in range(n):
        px = 100.0 + (i % 5) * 0.5
        out.append({"date": f"2026-06-{i % 28 + 1:02d}", "open": px, "high": px + 1,
                    "low": px - 1, "close": px, "adjusted_close": px,
                    "volume": (last_vol if i == n - 1 else 1000 + i)})
    return out


def test_indicators_zero_volume_last_bar_nulls_vol_metrics():
    """Битый бар фида (volume=0 последнего бара) → объёмные z = None + причина (П8);
    event_scan такой инструмент по объёму пропустит (сигнал не строится)."""
    good = C._indicators(_bars(40, last_vol=1500))
    assert good["vol_z_20"] is not None and good["vol_z_log_20"] is not None
    assert "vol_data_note" not in good
    broken = C._indicators(_bars(40, last_vol=0))
    assert broken["vol_z_20"] is None and broken["vol_z_log_20"] is None
    assert "нет данных" in broken["vol_data_note"]
    assert broken["ret_z_20"] is not None            # ценовая метрика не тронута
    # скан на битом объёме не строит объёмный сигнал, но строит ценовой
    out = ES.scan_events(indicators={"Z.US": broken})
    metrics = {s["метрика"] for s in out["сигналы"] if s["вид"] == "price"}
    assert metrics == {"ret_z_20"}                    # ни vol_z_log_20, ни vol_z_20


def test_scan_staleness_gate_drops_stale_bar():
    """Гейт давности бара: инструмент с протухшим последним баром выпадает из ценового скана
    при заданном asof_date; без asof_date (None) поведение прежнее (гейт выключен)."""
    fresh = {"asof": "2026-07-10", "ret_z_20": 3.0, "vol_z_log_20": 2.0}
    stale = {"asof": "2026-06-01", "ret_z_20": 3.0, "vol_z_log_20": 2.0}
    ind = {"F.US": fresh, "S.US": stale}
    # гейт выключен → оба инструмента в скане
    off = ES.scan_events(indicators=ind)
    assert {s["символ"] for s in off["сигналы"] if s["вид"] == "price"} == {"F.US", "S.US"}
    assert "протухшие_бары" not in off
    # гейт включён (asof 2026-07-13, порог 7 дней) → S.US протух, исключён + в реестре П8
    on = ES.scan_events(indicators=ind, asof_date="2026-07-13")
    assert {s["символ"] for s in on["сигналы"] if s["вид"] == "price"} == {"F.US"}
    assert [x["символ"] for x in on["протухшие_бары"]] == ["S.US"]
    assert on["протухшие_бары"][0]["давность_дней"] == 42


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
