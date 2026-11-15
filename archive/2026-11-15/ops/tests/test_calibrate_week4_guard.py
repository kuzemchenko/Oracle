# -*- coding: utf-8 -*-
"""Д1 #10: calibrate_week4 не должен молча стирать артефакт Д1 (fdr.tail_df / фон под открытую
вселенную) при регенерации thresholds.yaml. Герметично: tmp-файлы, боевой конфиг не трогается."""
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import calibrate_week4 as CW4        # noqa: E402

WITH_TAIL = """version: 2
fdr:
  q_value_max: 0.1
  background_metrics: {SPY.US: {ret: {std: 0.01, n: 100}}}
  tail_df:
    per_instrument: {SPY.US: {ret_z_20: 15.0}}
    fallback: {ret_z_20: 30.0}
timing: {spent_move_sigma: 1.5}
"""
WITHOUT_TAIL = "version: 2\nfdr:\n  q_value_max: 0.1\n  background_metrics: {}\ntiming: {}\n"


def test_refuse_when_tail_df_present_without_force(tmp_path):
    p = tmp_path / "thresholds.yaml"
    p.write_text(WITH_TAIL, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        CW4.preserve_d1_or_refuse(p, force=False)
    assert "Д1" in str(e.value) and "tail_df" in str(e.value)


def test_force_preserves_tail_df(tmp_path):
    p = tmp_path / "thresholds.yaml"
    p.write_text(WITH_TAIL, encoding="utf-8")
    td = CW4.preserve_d1_or_refuse(p, force=True)
    assert td["per_instrument"]["SPY.US"]["ret_z_20"] == 15.0
    assert td["fallback"]["ret_z_20"] == 30.0


def test_no_tail_df_returns_none(tmp_path):
    p = tmp_path / "thresholds.yaml"
    p.write_text(WITHOUT_TAIL, encoding="utf-8")
    assert CW4.preserve_d1_or_refuse(p, force=False) is None
    assert CW4.preserve_d1_or_refuse(tmp_path / "нет.yaml", force=False) is None


def test_write_thresholds_injects_preserved_tail_df(tmp_path, monkeypatch):
    """При preserve_tail_df=... секция уходит в fdr.tail_df собранного объекта (не теряется)."""
    captured = {}
    monkeypatch.setattr(CW4, "dump_yaml", lambda path, header, obj: captured.update(obj=obj))
    keep = {"per_instrument": {"XOM.US": {"ret_z_20": 8.0}}, "fallback": {"ret_z_20": 30.0}}
    CW4.write_thresholds(bg_grid={}, timing_pi={}, sig_def=1.5, vol_def=None, manip_pi={},
                         fb_def=3, sh_def=0.5, train_window={"from": "a", "to": "b", "symbols": 8},
                         preserve_tail_df=keep)
    assert captured["obj"]["fdr"]["tail_df"] == keep
