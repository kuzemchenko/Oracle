# -*- coding: utf-8 -*-
"""Тесты дашборда §15 (dashboard/build_dashboard).

Проверяет: присутствуют ВСЕ 7 метрик §15; калибровка/hit rate честно «накапливаются» без
форвард-исходов (П8, не выдуманный ноль); счётчик предотвращённых ошибок реально считается из
журналов воронки; реестр версий читает конфиги; HTML рендерится и содержит каждую секцию."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dashboard import build_dashboard as DB  # noqa: E402

M = DB.collect_metrics()


def test_all_seven_section15_metrics_present():
    for key in ("калибровка", "hit_rate_pnl", "стоимость_инсайта", "статус_воронки",
                "holdout", "реестр_версий", "предотвращённые_ошибки"):
        assert key in M, f"§15: отсутствует метрика {key}"


def test_calibration_honest_without_outcomes():
    cal = M["калибровка"]
    # на Нед.8 форвард-исходов нет → накапливается, но каркас корзин есть
    if cal["n_разрешённых"] == 0:
        assert cal["статус"] == "накапливается"
        assert len(cal["корзины"]) == 10
        assert all(b["n"] == 0 for b in cal["корзины"])
        assert cal["brier"] is None


def test_hitrate_pnl_dimensions():
    hp = M["hit_rate_pnl"]
    assert set(hp["разрезы"].keys()) == {"школы", "источники", "типы_идей"}


def test_cost_of_insight_fields():
    c = M["стоимость_инсайта"]
    for f in ("месячный_спенд_usd", "стоимость_на_прогон_usd", "стоимость_на_идею_usd",
              "идей_выдано", "потолок_usd_мес"):
        assert f in c
    assert c["потолок_usd_мес"] == 700


def test_prevented_errors_counted_from_journals():
    pe = M["предотвращённые_ошибки"]
    assert pe["всего_предотвращённых"] >= 0
    iz = pe["из_них"]
    assert set(iz.keys()) == {"отсев_FDR", "грубый_фильтр_тайминг_манипуляция",
                              "разбито_или_вето_в_дебатах", "процедурное_вето_П8"}
    # сумма компонент = всего
    assert sum(iz.values()) == pe["всего_предотвращённых"]
    # счётчик реально ненулевой (есть прогоны воронки с отсевом)
    assert pe["всего_предотвращённых"] > 0


def test_registry_reads_configs():
    r = M["реестр_версий"]
    assert r["веса_версия"] is not None
    assert r["pinned_quarter"]
    assert r["рубрика_версия"]


def test_holdout_budget():
    h = M["holdout"]
    assert h["бюджет_в_год"] == 4
    assert h["использовано"] + h["осталось"] == h["бюджет_в_год"]


def test_html_renders_all_sections():
    htmls = DB.render_html(M)
    for title_frag in ("Калибровочная кривая", "Hit rate", "Стоимость инсайта",
                       "Статус воронки", "holdout", "Реестр версий",
                       "предотвращённых ошибок"):
        assert title_frag in htmls, f"в HTML нет секции: {title_frag}"
