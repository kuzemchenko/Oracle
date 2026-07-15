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


def test_heavy_tail_p_is_more_conservative_than_normal():
    """F2#19: тот же z под t-нулём даёт БОЛЬШИЙ p, чем старый нормальный erfc → меньше ложных FDR."""
    import math
    sigs = ES.price_vol_signals({"X.US": {"ret_z_20": 3.2, "vol_z_log_20": 3.2}})
    by_metric = {s["метрика"]: s for s in sigs}
    erfc_p = math.erfc(3.2 / math.sqrt(2))
    assert by_metric["ret_z_20"]["p_value"] > erfc_p          # t(5) тяжелее нормали
    assert by_metric["vol_z_log_20"]["p_value"] > erfc_p      # t(6) тяжелее нормали
    assert all(s.get("df_нуля") for s in sigs)                # нуль помечен


def test_volume_prefers_log_metric_with_raw_fallback():
    """F2#19: при наличии vol_z_log_20 берём его; иначе — фолбэк на сырой vol_z_20 (df=3)."""
    log_sig = ES.price_vol_signals({"A.US": {"ret_z_20": 0.1, "vol_z_log_20": 2.0, "vol_z_20": 9.9}})
    vmetrics = {s["метрика"] for s in log_sig if "vol" in s["метрика"]}
    assert vmetrics == {"vol_z_log_20"}                       # сырой 9.9 проигнорирован в пользу лог
    raw_sig = ES.price_vol_signals({"B.US": {"ret_z_20": 0.1, "vol_z_20": 2.0}})  # лог отсутствует
    vraw = [s for s in raw_sig if s["метрика"] == "vol_z_20"]
    assert len(vraw) == 1 and vraw[0]["df_нуля"] == 3         # фолбэк на сырой с тяжёлым df=3


def test_variant2_candidate_survives_failed_fdr():
    """Д1-Вариант2: всплеск тренда, который строгий BH НЕ пропускает (пул раздут ценовым шумом),
    всё равно доходит до суда как топ-кандидат канала. FDR-ярлык при этом честно False."""
    # 20 шумовых инструментов раздувают m → планка BH-одиночки недостижима для тренда (пол p≈0.036).
    noise = {f"N{i}.US": {"ret_z_20": 0.2, "vol_z_20": 0.1} for i in range(20)}
    r = ES.scan_events(news=[], trends_rows=_trends(), indicators=noise, q_max=0.1)
    lithium = [s for s in r["сигналы"] if s.get("ключ") == "lithium supply"]
    assert lithium and lithium[0]["сигнал_после_FDR"] is False   # строгий FDR не пропустил
    assert lithium[0]["кандидат"] is True                        # но это топ-кандидат канала
    labels = [e["метка"] for e in r["кандидат_события"]]
    assert any("lithium" in (lbl or "") for lbl in labels)       # дошёл до кандидат-событий
    assert r["кандидатов_к_суду"] >= 1


def test_variant2_candidate_cap_per_channel():
    """Д1-Вариант2: кап ширины — не больше CAND_PRICE_TOP ЗАМЕТНЫХ (p<0.05) ценовых кандидатов,
    отбор по значимости. Шум раздувает m → строгий FDR никого не пропускает (суперсет не мешает капу)."""
    # 20 заметных (|z| 2.6..3.0, p<0.05) + 100 шумовых (|z|=0.2) → BH отвергает 0 (планка q/m мала).
    inds = {f"S{i:02d}.US": {"ret_z_20": 2.6 + 0.02 * i, "vol_z_20": 0.0} for i in range(20)}
    inds.update({f"N{i:03d}.US": {"ret_z_20": 0.2, "vol_z_20": 0.1} for i in range(100)})
    r = ES.scan_events(news=[], trends_rows=[], indicators=inds, q_max=0.1)
    assert r["статистических_после_FDR"] == 0                    # строгий FDR пуст → суперсет не влияет
    price_cand = [s for s in r["сигналы"] if s.get("вид") == "price" and s.get("кандидат")]
    assert len(price_cand) == ES.CAND_PRICE_TOP                  # ровно кап (заметных 20 > капа 15)
    cand_syms = {s["символ"] for s in price_cand}
    assert "S19.US" in cand_syms and "S00.US" not in cand_syms   # самый аномальный да, слабейший нет


def test_variant2_notability_floor_quiet_day():
    """Д1-Вариант2: в ШТИЛЬ (все |z| малы, p≥0.05) кандидатов НЕТ — событийная чувствительность,
    не «топ-N шевелящихся каждый день». Это и есть честный пустой день (§6)."""
    inds = {f"Q{i:02d}.US": {"ret_z_20": 0.5, "vol_z_20": 0.3} for i in range(30)}  # |z| мелкие
    r = ES.scan_events(news=[], trends_rows=[], indicators=inds, q_max=0.1)
    assert r["кандидатов_к_суду"] == 0
    assert r["кандидат_события"] == []


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


# ── Этап2: бирка статистической силы канала (ярлык, не фильтр) ─────────────────────────
def test_strength_badge_buckets():
    assert ES._strength_from_p(0.001) == "сильный"
    assert ES._strength_from_p(0.01) == "средний"
    assert ES._strength_from_p(0.04) == "слабый"
    assert ES._strength_from_p(None) is None


def test_news_strength_degenerate_day_not_forced_weak():
    """stage-review Этап2: единственный/равносалиентный кластер → сила НЕ измерена (None), а не
    принудительный «слабый» (иначе сильное одиночное событие лгало бы «слабый»)."""
    single = [{"салиентность": 9}]
    ES._tag_news_strength(single)
    assert single[0]["сила_сигнала"] is None
    equal = [{"салиентность": 5}, {"салиентность": 5}]
    ES._tag_news_strength(equal)
    assert all(e["сила_сигнала"] is None for e in equal)
    varied = [{"салиентность": 9}, {"салиентность": 1}]
    ES._tag_news_strength(varied)
    assert varied[0]["сила_сигнала"] == "сильный" and varied[1]["сила_сигнала"] == "слабый"


def test_candidates_carry_strength_badge():
    """Каждый кандидат-событие несёт бирку силы (для протокола и «Разбора дня» Этапа3)."""
    inds = {f"S{i:02d}.US": {"ret_z_20": 2.6 + 0.05 * i, "vol_z_20": 0.0} for i in range(10)}
    r = ES.scan_events(news=[], trends_rows=[], indicators=inds, q_max=0.1)
    cand_events = [e for e in r["кандидат_события"] if e["вид"] == "price"]
    assert cand_events and all(e.get("сила_сигнала") in ("сильный", "средний", "слабый")
                               for e in cand_events)
