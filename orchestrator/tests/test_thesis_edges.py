# -*- coding: utf-8 -*-
"""Тесты V6 «рёбра из тезисов» (orchestrator/thesis_edges.py). Герметично: tmp-реестр,
стаб-библиотека, конфиг/тезисы/механизмы — tmp-файлы.

Закрепляют: валидация (тикер вне универсума / неизвестный механизм / пустая
причинность / дубль — не идут), append с origin=thesis и провенансом, дедуп против
библиотеки и реестра, идемпотентность повторного прогона, подхват edge_library
с origin=thesis ПОСЛЕ библиотечных."""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import thesis_edges as TE      # noqa: E402
from orchestrator import edge_forward as EFW     # noqa: E402

UNI = {"USO.US", "FRO.US", "BNO.US", "LNG.US"}


def _cfg(tmp_path, rows):
    p = tmp_path / "thesis_edges.yaml"
    p.write_text(yaml.safe_dump({"рёбра": rows}, allow_unicode=True), encoding="utf-8")
    return p


def _aux(tmp_path):
    th = tmp_path / "theses.yaml"
    th.write_text(yaml.safe_dump({"тезисы": {"T1_потоки_критических_активов": {}}},
                                 allow_unicode=True), encoding="utf-8")
    ms = tmp_path / "mechs.yaml"
    ms.write_text(yaml.safe_dump({"механизмы": [{"id": "energy_supply_shock"}]},
                                 allow_unicode=True), encoding="utf-8")
    sens = tmp_path / "sens.yaml"
    sens.write_text(yaml.safe_dump({"sensitivities": [
        {"источник": "USO.US", "узел": "BNO.US", "lag": 0}]}, allow_unicode=True),
        encoding="utf-8")
    return th, ms, sens


def _row(src="USO.US", dst="FRO.US", **over):
    r = {"тезис": "T1_потоки_критических_активов", "механизм": "energy_supply_shock",
         "источник": src, "инструмент": dst, "причинность": "шок → фрахт"}
    r.update(over)
    return r


def test_validation_rejects_bad_rows(tmp_path):
    th, ms, _ = _aux(tmp_path)
    rows = [_row(),                                        # ок
            _row(dst="ZZZZ.US"),                           # вне универсума
            _row(механизм="нет_такого"),                   # неизвестный механизм
            _row(причинность="  "),                        # П8: пустая причинность
            _row(),                                        # дубль внутри конфига
            _row(src="BNO.US", dst="BNO.US")]              # источник=инструмент
    ok, errors = TE.validate(rows, universe=UNI, theses_path=th, mechs_path=ms)
    assert len(ok) == 1 and len(errors) == 5
    txt = " | ".join(errors)
    assert "вне универсума" in txt and "не найден в mechanisms" in txt
    assert "причинность" in txt and "дубль" in txt and "источник = инструмент" in txt


def test_sync_appends_dedups_and_is_idempotent(tmp_path):
    th, ms, sens = _aux(tmp_path)
    cfg = _cfg(tmp_path, [_row(),                           # новое
                          _row(src="USO.US", dst="BNO.US")])  # дубль БИБЛИОТЕКИ (sens)
    reg = tmp_path / "candidates.jsonl"
    s = TE.sync(cfg, registry_path=reg, sens_path=sens, universe=UNI,
                theses_path=th, mechs_path=ms)
    assert s["добавлено"] == ["USO.US→FRO.US"] and s["дубль_библиотеки"] == 1
    rec = json.loads(reg.read_text("utf-8").splitlines()[0])
    assert rec["origin"] == "thesis" and rec["тезис"].startswith("T1_")
    assert rec["механизм"] == "energy_supply_shock" and "тезисов владельца" in rec["провенанс"]
    # идемпотентность: повтор ничего не дублирует
    s2 = TE.sync(cfg, registry_path=reg, sens_path=sens, universe=UNI,
                 theses_path=th, mechs_path=ms)
    assert s2["добавлено"] == [] and s2["дубль_реестра"] == 1
    assert len(reg.read_text("utf-8").splitlines()) == 1
    # edge_library подхватывает с origin=thesis ПОСЛЕ библиотечных
    edges = EFW.edge_library(sens, candidates_path=reg)
    assert [e["origin"] for e in edges] == ["library", "thesis"]
    assert edges[1]["from"] == "USO.US" and edges[1]["to"] == "FRO.US"


def test_battle_config_is_valid():
    """Регрессия: боевой knowledge/thesis_edges.yaml валиден целиком."""
    ok, errors = TE.validate(TE.load_config())
    assert errors == [], errors
    assert len(ok) >= 10
