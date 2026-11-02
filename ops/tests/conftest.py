# -*- coding: utf-8 -*-
"""Предохранитель тестов ops/: НИ ОДИН тест не пишет боевые журналы рабочей копии (П16).

Инцидент 02.11: бот импортирует ops.hunter_seal, тест подменил путь у hunter_seal (другое имя
того же файла) — печать ушла в journal/predictions.jsonl облачной копии (откатано git checkout,
сервер не затронут). Теперь пути журнала печати охотника перенаправляются во временную папку
для ОБОИХ имён модуля, автоматически для каждого теста."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "ops")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _no_battle_journal_writes(tmp_path, monkeypatch):
    import importlib
    for name in ("ops.hunter_seal", "hunter_seal"):
        mod = importlib.import_module(name)
        monkeypatch.setattr(mod, "PRED", tmp_path / "guard_predictions.jsonl")
        monkeypatch.setattr(mod, "OUTC", tmp_path / "guard_outcomes.jsonl")
    yield
