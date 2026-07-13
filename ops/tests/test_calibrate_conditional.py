# -*- coding: utf-8 -*-
"""Смоук-тест драйвера ops/calibrate_conditional.py (этап Д3): на маленькой синтетической БД
драйвер честно отрабатывает все пары («нет данных» — легитимно, П8), пишет YAML со статусным
header'ом «КАЛИБРОВОЧНО-СПРАВОЧНЫЙ» (прецедент FГ2 §9.1) и отчёт с блоком устойчивости
к порогу. Боевые файлы не трогаются (пути monkeypatch'атся в tmp)."""
import sqlite3
import sys
import pathlib

import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops import calibrate_conditional as CC     # noqa: E402


def _make_db(path, n=1400):
    rng = np.random.default_rng(9)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, "
                "close REAL, adjusted_close REAL, volume REAL, source TEXT, fetched_at TEXT)")
    dates = [f"{2020 + i // 250}-{(i % 250) // 21 + 1:02d}-{(i % 21) + 1:02d}" for i in range(n)]
    src_ret = rng.normal(0.0, 0.01, n)
    for sym, ret in (("USO.US", src_ret),
                     ("BNO.US", 0.9 * src_ret + rng.normal(0.0, 0.003, n))):
        px = 50.0 * np.exp(np.cumsum(ret))
        con.executemany(
            "INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(sym, d, p, p, p, p, p, 1e6, "test", d) for d, p in zip(dates, px)])
    con.commit()
    con.close()


def test_driver_writes_reference_yaml_and_report(tmp_path, monkeypatch):
    db = tmp_path / "q.db"
    _make_db(str(db))
    monkeypatch.setattr(CC, "OUT_YAML", tmp_path / "conditional_sensitivities.yaml")
    monkeypatch.setattr(CC, "REPORTS", tmp_path / "reports")
    res = CC.calibrate(db=str(db), robustness=True)
    CC.write(res)

    assert res["n_pairs"] == res["n_established"] + res["n_not_established"]
    by_pair = {(r["источник"], r["узел"]): r for r in res["conditional_sensitivities"]}
    # контрольная пара присутствует и измерена (в синтетике перенос синхронный и сильный)
    uso_bno = by_pair[("USO.US", "BNO.US")]
    assert uso_bno["status"] in ("установлено", "не установлено")
    # символы без данных в этой БД → честное «не установлено»/«нет данных», не выдумка (П8)
    absent = [r for r in res["conditional_sensitivities"]
              if r["источник"] not in ("USO.US", "BNO.US") or r["узел"] not in ("USO.US", "BNO.US")]
    assert absent and all(r["status"] == "не установлено" for r in absent)
    # блок устойчивости: все три порога посчитаны для каждой пары
    assert res["robustness"] and all(
        set(row) >= {"источник", "узел", "0.4σ", "0.5σ", "0.6σ"} for row in res["robustness"])

    text = (tmp_path / "conditional_sensitivities.yaml").read_text(encoding="utf-8")
    assert "КАЛИБРОВОЧНО-СПРАВОЧНЫЙ" in text          # header-прецедент FГ2 §9.1
    assert "НЕ вход живого" in text
    doc = yaml.safe_load(text)
    assert doc["n_pairs"] == res["n_pairs"]
    assert all("folds" not in r for r in doc["conditional_sensitivities"])  # детали — в report.json
    report = (tmp_path / "reports" / "REPORT.md").read_text(encoding="utf-8")
    assert "Устойчивость выводов к порогу" in report
    assert "Маппинг N_эпизодов → ярус" in report
    assert (tmp_path / "reports" / "report.json").exists()


def test_collect_pairs_includes_court_event_controls():
    pairs = {(p["источник"], p["узел"]) for p in CC.collect_pairs()}
    assert ("USO.US", "BNO.US") in pairs               # контроль «известная сильная связь»
    assert ("XOM.US", "EEM.US") in pairs               # вердикт суда r²=0.0
    for t in ("FRO.US", "STNG.US", "DHT.US"):          # новостные кейсы танкеров
        assert ("BNO.US", t) in pairs
