# -*- coding: utf-8 -*-
"""Ре-гейт №2 02.11: контракт писатель/читатель для «полный_ход».

Закрепляет: node_to_facts кладёт СЫРОЙ amplitude_total узла ПОД КЛЮЧОМ edge_total и НЕ
создаёт ключа amplitude_total — поэтому канон читателя (_node_brief в event_first):
полный_ход = facts["edge_total"]; «фолбэки» на facts["amplitude_total"] — мёртвый код
(два таких уже сняты: 01.11 dict.get-default и 02.11 «по значению»). Дрейф контракта
(переименование/потеря ключа) ломает подачу направления/хода — ловится здесь."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import graph_build as GB  # noqa: E402


def _facts(node, monkeypatch):
    monkeypatch.setattr(GB, "_isolation_r2_and_sigma", lambda con, root, sym: (None, None))
    monkeypatch.setattr(GB, "_adv", lambda con, sym: None)
    monkeypatch.setattr(GB.U, "is_sealable", lambda sym, con=None: True)
    return GB.node_to_facts(node, con=None, horizon_days=5)


def test_edge_total_is_raw_amplitude_total(monkeypatch):
    f = _facts({"узел": "LNG.US", "amplitude_total": 7.5, "amplitude": 2.0}, monkeypatch)
    assert f["edge_total"] == 7.5
    assert "amplitude_total" not in f, \
        "в facts появился второй ключ — синхронизируй читателя event_first (полный_ход)"


def test_edge_total_none_is_honest(monkeypatch):
    f = _facts({"узел": "LNG.US", "amplitude_total": None, "amplitude": None}, monkeypatch)
    assert f["edge_total"] is None          # None честный: корень upstream (compose_chain)
