# -*- coding: utf-8 -*-
"""БАГ#13: headline-метрика прогона = РЕАЛЬНЫЙ поток (картограф + граф-топ), НЕ len(top3).

Под --vet (skip_contour=True) дорогой 21-агентный контур по шок-источникам НЕ запускается, поэтому
ЛЕГАСИ-выдача top3 ≡ 0. Раньше строка "итог"/PROG/резюме сообщали «контур выдал 0 идей», хотя реальные
идеи текут через картограф + узлы графа (топ_k → треки money/провизорный/дайджест). Здесь доказываем,
что при ненулевых картограф_идеи/топ_k метрика потока в "итог" НЕнулевая и НЕ завязана на top3.

Тяжёлый конвейер (скан/каскады/добор/суд/seal) замокан — тест точечный и сети не трогает.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_first as EF    # noqa: E402


def _fake_scan():
    return {"источники": ["mock"], "сырых_сигналов": 0, "статистических_после_FDR": 0,
            "кандидат_события": []}


def _fake_node_sel(symbol):
    """Минимальный элемент ранжирования воронки отбора (структура для _node_brief)."""
    return {"symbol": symbol, "score": 0.5,
            "node": {"amplitude": 0.1, "_chain": None, "research": False},
            "prerank": {"reliability": "—"}}


def test_flow_metric_nonzero_under_skip_contour(monkeypatch):
    # --- скан/источники: контур по источникам НЕ нужен (skip_contour=True) ---
    monkeypatch.setattr(EF.ES, "scan_events_live", lambda **kw: _fake_scan())
    monkeypatch.setattr(EF, "_shock_sources", lambda *a, **k: [])
    monkeypatch.setattr(EF, "_price_signal_syms", lambda scan: [])
    # --- картограф: НЕнулевой поток research-идей (вне реестра тем) ---
    monkeypatch.setattr(EF, "_cartographer_pass", lambda *a, **k: [])
    monkeypatch.setattr(EF, "_stage_cartographer", lambda pcs, now: [])
    monkeypatch.setattr(EF, "_proposal_ideas",
                        lambda proposals: [{"актив": "AAA"}, {"актив": "BBB"}])  # 2 картограф-идеи
    monkeypatch.setattr(EF, "activated_chains", lambda *a, **k: [])
    # --- воронка отбора графа: НЕнулевой топ_k (3 узла → треки) ---
    топ = [_fake_node_sel(s) for s in ("XXX", "YYY", "ZZZ")]
    monkeypatch.setattr(EF.GB, "select_from_nodes",
                        lambda *a, **k: {"всего": 3, "ворота_прошли": 3,
                                         "отсев_по_критериям": {}, "топ_k": топ, "ранжировано": топ})
    monkeypatch.setattr(EF.GB, "route_tracks",
                        lambda sel: {"money": топ[:1], "provisional": топ[1:], "digest_only": []})

    p = EF.run_event_first(mode="mock", k=2, write=False, skip_contour=True)

    fl = p["поток_идей"]
    # ЛЕГАСИ-контур пуст под skip_contour — но это НЕ headline.
    assert fl["состязательный_контур"] == 0
    assert fl["контур_включён"] is False
    assert len(p["контур_выдал_топ3"]) == 0
    # HEADLINE-поток НЕнулевой = картограф(2) + граф-топ(3) = 5, и НЕ равен top3.
    assert fl["картограф"] == 2
    assert fl["граф_топ"] == 3
    assert fl["всего"] == 5
    assert fl["всего"] != fl["состязательный_контур"]
    # Строка "итог" сообщает реальный поток, а не «контур выдал 0 идей».
    assert "идей в поток 5" in p["итог"]
    assert "картограф 2 + граф-топ 3" in p["итог"]
    assert "состязательный контур 0 идей (выключен под --vet)" in p["итог"]


def test_flow_metric_dedup_overlap(monkeypatch):
    """ДОЛГ stage-review F1: поток = УНИКАЛЬНЫЕ активы. Картограф-цепочки питают И картограф_идеи, И
    граф→топ_k; один тикер не должен считаться дважды (раньше headline завышался). Здесь LRCX в ОБОИХ
    множествах: картограф {LRCX, AAA} ∪ граф {LRCX, YYY, ZZZ} → 4 уникальных, пересечение 1."""
    monkeypatch.setattr(EF.ES, "scan_events_live", lambda **kw: _fake_scan())
    monkeypatch.setattr(EF, "_shock_sources", lambda *a, **k: [])
    monkeypatch.setattr(EF, "_price_signal_syms", lambda scan: [])
    monkeypatch.setattr(EF, "_cartographer_pass", lambda *a, **k: [])
    monkeypatch.setattr(EF, "_stage_cartographer", lambda pcs, now: [])
    monkeypatch.setattr(EF, "_proposal_ideas",
                        lambda proposals: [{"актив": "LRCX"}, {"актив": "AAA"}])  # LRCX пересечётся
    monkeypatch.setattr(EF, "activated_chains", lambda *a, **k: [])
    топ = [_fake_node_sel(s) for s in ("LRCX", "YYY", "ZZZ")]   # LRCX и в графе
    monkeypatch.setattr(EF.GB, "select_from_nodes",
                        lambda *a, **k: {"всего": 3, "ворота_прошли": 3,
                                         "отсев_по_критериям": {}, "топ_k": топ, "ранжировано": топ})
    monkeypatch.setattr(EF.GB, "route_tracks",
                        lambda sel: {"money": топ[:1], "provisional": топ[1:], "digest_only": []})

    p = EF.run_event_first(mode="mock", k=2, write=False, skip_contour=True)

    fl = p["поток_идей"]
    # Сырые per-source счётчики сохранены, но "всего" = union уникальных (НЕ 2+3=5).
    assert fl["картограф"] == 2
    assert fl["граф_топ"] == 3
    assert fl["пересечение"] == 1            # LRCX в обоих
    assert fl["всего"] == 4                  # {LRCX, AAA, YYY, ZZZ}
    assert "идей в поток 4 уникальных" in p["итог"]
    assert "−1 пересеч." in p["итог"]
