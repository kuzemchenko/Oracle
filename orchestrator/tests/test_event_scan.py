# -*- coding: utf-8 -*-
"""Тесты открытого event-first скана (orchestrator/event_scan.py, Этап 1 §6).

Проверяет:
  • три ОТКРЫТЫХ источника (новости/тренды/цена) сводятся в один пул, скан НЕ привязан к тикерам;
  • единый FDR по статистическим сигналам (цена+тренды); экстремальная z-аномалия проходит;
  • новостные кластеры ранжируются по салиентности и НЕ отбрасываются, с честным П8 про частотный FDR;
  • открытый ключ тренда (вне универсума из 14) может стать кандидат-событием — доказательство открытости;
  • живой смоук из БД через ДИНАМИЧЕСКИЙ sealable_universe (если есть storage/oracle.db).
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_scan as ES   # noqa: E402
from orchestrator import context as C        # noqa: E402


def _news():
    # два различимых кластера + одиночка
    return ([{"title": "Iran oil exports surge as sanctions ease deal nears"}] * 4 +
            [{"title": "Datacenter power demand strains electric grid transformers"}] * 3 +
            [{"title": "Local weather forecast sunny weekend"}])


def _trends():
    rows = []
    rows += [("lithium supply", f"2026-05-{d:02d}", 50) for d in range(1, 28)]   # вне 14-универсума
    rows += [("lithium supply", "2026-05-28", 99)]                                # всплеск
    rows += [("calm topic", f"2026-05-{d:02d}", 50) for d in range(1, 29)]        # ровный фон
    return rows


def _indicators():
    return {"BNO.US": {"ret_z_20": 4.6, "vol_z_20": 0.2},   # экстремальная аномалия → пройдёт FDR
            "SPY.US": {"ret_z_20": 0.3, "vol_z_20": -0.1}}  # шум


def test_open_three_sources_and_fdr():
    r = ES.scan_events(news=_news(), trends_rows=_trends(), indicators=_indicators(), q_max=0.1)
    assert r["discovery_open"] is True
    src = r["источники"]
    assert src["news_clusters"] >= 2 and src["trends"] >= 1 and src["price"] >= 1
    # экстремальная z=4.6 обязана пройти единый FDR
    assert r["статистических_после_FDR"] >= 1
    passed = [s for s in r["сигналы"] if s["сигнал_после_FDR"]]
    assert any(s.get("символ") == "BNO.US" and s["метрика"] == "ret_z_20" for s in passed)


def test_news_clusters_ranked_not_dropped_with_p8():
    r = ES.scan_events(news=_news(), trends_rows=[], indicators={}, q_max=0.1)
    assert len(r["новостные_события"]) >= 2          # кластеры не отброшены
    assert r["новостные_события"][0]["салиентность"] >= r["новостные_события"][-1]["салиентность"]
    assert "частотный FDR" in r["ограничение_П8"] and "null" in r["ограничение_П8"]


def test_openness_trend_keyword_outside_universe():
    """Ключ тренда 'lithium supply' нет среди 14 — но он может стать кандидат-событием (открытость)."""
    r = ES.scan_events(news=[], trends_rows=_trends(), indicators={}, q_max=0.5)
    labels = [e["метка"] for e in r["кандидат_события"]]
    # при мягком q всплеск lithium проходит и попадает в события — вселенная не ограничивает открытие
    assert any("lithium" in (lbl or "") for lbl in labels)


def test_empty_scan_is_legitimate():
    r = ES.scan_events(news=[], trends_rows=[], indicators={}, q_max=0.1)
    assert r["сырых_сигналов"] == 0
    assert r["кандидат_события"] == []
    assert r["статистических_после_FDR"] == 0


@pytest.mark.skipif(not C.DB.exists(), reason="нет storage/oracle.db для живого смоука")
def test_live_smoke():
    r = ES.scan_events_live(q_max=0.1, news_limit=300)
    assert r["discovery_open"] is True
    assert "источники" in r and "кандидат_события" in r
    assert r["источники"]["news_clusters"] >= 0   # структура валидна на живых данных
