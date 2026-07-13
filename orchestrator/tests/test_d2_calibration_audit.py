# -*- coding: utf-8 -*-
"""Д2 (ROADMAP_2026-07) — regression-тесты фикса конвенции горизонта калибровочного трека.

Найденный аудитом Д2 минорный баг (НЕ первопричина hit 36.1% — см. ops/reports/d2_audit/AUDIT.md):
порог печати отсчитывался от ЯКОРНОГО close (последний бар БД, обычно вчера), а resolve_by —
от МОМЕНТА ПЕЧАТИ (+7 календарных дней). Итог: фактический горизонт «якорный бар → бар исхода»
был 5–7 торговых баров при заявке вероятности под σ·√5 (в журнале: 5×156, 6×78, 7×18 из 252).

Тест воспроизводит ошибку на синтетических данных ТОЙ ЖЕ ФОРМЫ, что зафиксированная печать
журнала NUE.US run=calibrate_20260623T080001Z (якорь Пн 2026-06-22, печать Вт 23.06 08:00,
resolve_by 30.06 → бар исхода = 6-й бар после якоря), и проверяет фикс: resolve_by теперь
= якорь + 7 дней → ровно 5 будних дней/баров. Старые печати не переписываются (П16)."""
import datetime
import math
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import calibrate as CAL    # noqa: E402
from mathlib import sealing as SEAL          # noqa: E402

UTC = datetime.timezone.utc


def _weekdays(start, n):
    """n будних дат подряд, начиная со start (сам start — будний)."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _make_db(anchor_date, n_bars=80, symbol="SPY.US"):
    """Синтетическая БД котировок НА МОМЕНТ ПЕЧАТИ: n_bars будних баров, ПОСЛЕДНИЙ =
    anchor_date (как в бою — будущих баров в БД нет). Будущие будние даты возвращаются
    отдельно — только для арифметики «какой бар возьмёт сверка» (в БД не пишутся)."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, "
                "adjusted_close REAL, volume INTEGER)")
    start = anchor_date - datetime.timedelta(days=int(n_bars * 7 / 5) + 10)
    dates = [d for d in _weekdays(start, 200) if d <= anchor_date][-n_bars:]
    assert dates[-1] == anchor_date
    future = _weekdays(anchor_date + datetime.timedelta(days=1), 15)
    px = 100.0
    for i, d in enumerate(dates):
        px *= math.exp(0.001 * ((-1) ** i))            # детерминированные малые колебания
        con.execute("INSERT INTO quotes VALUES (?,?,?,?,?)",
                    (symbol, d.isoformat(), round(px, 4), round(px, 4), 1000))
    con.commit()
    return con, dates, future


def _bars_between(all_dates, anchor, resolve_date):
    """Номер бара исхода после якоря по конвенции сверки: первый бар с датой >= resolve_date."""
    obs = next(d for d in all_dates if d >= resolve_date)
    return sum(1 for d in all_dates if anchor < d <= obs)


def test_d2_regression_old_convention_gave_6_bars_new_gives_5():
    # форма зафиксированной печати: якорь Пн 2026-06-22, печать Вт 23.06 08:00 UTC
    anchor = datetime.date(2026, 6, 22)
    now = datetime.datetime(2026, 6, 23, 8, 0, tzinfo=UTC)
    con, dates, future = _make_db(anchor)
    all_dates = dates + future

    # СТАРАЯ конвенция (до Д2): resolve_by = печать + 7 дней → 30.06 → 6-й бар после якоря.
    old_resolve = (now + datetime.timedelta(days=7)).date()
    assert old_resolve == datetime.date(2026, 6, 30)
    assert _bars_between(all_dates, anchor, old_resolve) == 6   # ← воспроизведённая ошибка

    # НОВАЯ конвенция (фикс Д2): resolve_by = якорь + 7 дней → 29.06 → ровно 5-й бар.
    preds = CAL.build_calibration_predictions(con, "t", now_dt=now)
    con.close()
    assert preds, "печати должны построиться"
    for p in preds:
        assert p["threshold_asof_close_date"] == anchor.isoformat()
        assert p["resolve_by"] == "2026-06-29T20:00:00+00:00"   # якорь + 7 дней, 20:00 UTC
        rd = datetime.date.fromisoformat(p["resolve_by"][:10])
        assert _bars_between(all_dates, anchor, rd) == 5        # заявка σ·√5 согласована
        assert "horizon_anchor" in p                            # провенанс фикса в печати


def test_d2_probabilities_unchanged_and_resolvable():
    anchor = datetime.date(2026, 6, 22)
    now = datetime.datetime(2026, 6, 23, 8, 0, tzinfo=UTC)
    con, _, _ = _make_db(anchor)
    preds = CAL.build_calibration_predictions(con, "t", now_dt=now)
    con.close()
    assert {p["probability"] for p in preds} == {0.5, 0.3085, 0.6915}   # три уровня, НЕ «все 0.5»
    for p in preds:
        assert not SEAL.validate_resolvable(p)                          # §9-разрешимы
        assert p["resolve_by"] > now.isoformat()                        # строго в будущем (П16)


def test_d2_stale_anchor_skipped():
    # якорь старше MAX_ANCHOR_STALENESS_DAYS → честный пропуск (иначе resolve_by уехал бы в прошлое)
    anchor = datetime.date(2026, 6, 22)
    now = datetime.datetime(2026, 6, 29, 8, 0, tzinfo=UTC)              # 7 дней после якоря
    con, _, _ = _make_db(anchor)
    preds = CAL.build_calibration_predictions(con, "t", now_dt=now)
    con.close()
    assert preds == []


def test_d2_fresh_anchor_within_guard_still_prints():
    anchor = datetime.date(2026, 6, 22)
    now = datetime.datetime(2026, 6, 24, 8, 0, tzinfo=UTC)              # staleness 2 ≤ 4
    con, _, _ = _make_db(anchor)
    preds = CAL.build_calibration_predictions(con, "t", now_dt=now)
    con.close()
    assert len(preds) == 3
    assert all(p["resolve_by"] == "2026-06-29T20:00:00+00:00" for p in preds)
