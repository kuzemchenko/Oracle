# -*- coding: utf-8 -*-
"""V2 Q-темы (09.11): одобренные темы → config/universe.yaml с провенансом, прежние темы целы."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops import apply_themes as AT   # noqa: E402

UNI = """quote_source: eodhd
themes:
  brent:
    proxy_etf: BNO.US   # комментарий
core_symbols: [SPY.US]
"""


def _dec(tmp_path, rows):
    p = tmp_path / "td.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return p


def test_apply_approved_only_last_decision_wins(tmp_path):
    import yaml
    u = tmp_path / "universe.yaml"
    u.write_text(UNI, encoding="utf-8")
    d = _dec(tmp_path, [
        {"type": "theme_decision", "ключ": "Copper-Grid", "решение": "rejected", "инструменты": ["FCX.US"]},
        {"type": "theme_decision", "ключ": "Copper-Grid", "решение": "approved", "инструменты": ["FCX.US", "SCCO.US"],
         "кластер_ключи": ["copper", "grid"], "событие": "дефицит меди", "ts": "2026-11-14T10:00:00+00:00"},
        {"type": "theme_decision", "ключ": "x2", "решение": "rejected", "инструменты": ["A.US"]}])
    dry = AT.apply(u, d, write=False)
    assert dry["добавлено"] == ["owner_copper_grid"] and u.read_text("utf-8") == UNI
    res = AT.apply(u, d, write=True)
    cfg = yaml.safe_load(u.read_text("utf-8"))
    assert res["записано"] and set(cfg["themes"]) == {"brent", "owner_copper_grid"}
    t = cfg["themes"]["owner_copper_grid"]
    assert t["related"] == ["FCX.US", "SCCO.US"] and "одобрено владельцем 2026-11-14" in t["provenance"]
    assert cfg["core_symbols"] == ["SPY.US"] and "# комментарий" in u.read_text("utf-8")
    assert AT.apply(u, d, write=True)["добавлено"] == []          # повтор не дублирует


def test_no_decisions_no_change(tmp_path):
    u = tmp_path / "universe.yaml"
    u.write_text(UNI, encoding="utf-8")
    assert AT.apply(u, tmp_path / "нет.jsonl")["добавлено"] == []
