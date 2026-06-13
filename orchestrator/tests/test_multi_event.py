# -*- coding: utf-8 -*-
"""Тесты мульти-событийного режима (orchestrator/multi_event.py, §17.1 апгрейд)."""
from orchestrator import multi_event as ME


def test_detect_news_clusters_groups_similar_titles():
    news = [{"title": "SpaceX IPO soars on debut day"},
            {"title": "SpaceX debut day: shares soar at IPO"},   # тот же сюжет
            {"title": "Copper prices fall on China demand"}]
    cl = ME.detect_news_clusters(news, threshold=0.2)
    sizes = sorted(c["size"] for c in cl)
    assert sizes == [1, 2]                       # два SpaceX схлопнулись, медь отдельно


def test_rank_events_orders_by_tectonic_and_marks_far_node():
    r = ME.rank_events(news=[])
    events = {e["id"]: e for e in r["события"]}
    assert "ai_power" in events and "spacex" in events
    # ai_power имеет карту каскада → несёт тектонику с дальним узлом CLF
    assert events["ai_power"]["tectonic"] is not None
    assert "CLF.US" in events["ai_power"]["tectonic"]["далёкий_узел"]["instruments"]
    # ai_power (тектоника ~0.71) приоритетнее калибровочной brent (0.4)
    assert events["ai_power"]["score"] > events["brent"]["score"]


def test_news_match_boosts_topical_theme():
    # кластер про spacex должен поднять приоритет темы spacex (актуальна сегодня)
    news = [{"title": "SpaceX starlink IPO debut"}, {"title": "SpaceX starlink debut soars"}]
    r = ME.rank_events(news=news)
    spacex = next(e for e in r["события"] if e["id"] == "spacex")
    assert spacex["score"] >= 0.6                # база + возможная прибавка за новости


def test_run_multi_event_mock_diversifies_and_surfaces_clusters():
    p = ME.run_multi_event(mode="mock", k=2, write=False)
    assert p["глубоко_проанализировано"][:2]           # топ-2 темы обработаны
    assert len(p["объединённая_выдача_топ3"]) <= 3       # диверсифицированная выдача
    assert "ранжирование_событий" in p and "обнаруженные_кластеры_новостей" in p
    # диверсификация: активы в выдаче не повторяются (одна ставка на драйвер §4)
    assets = [i["актив"] for i in p["объединённая_выдача_топ3"]]
    assert len(assets) == len(set(assets))
