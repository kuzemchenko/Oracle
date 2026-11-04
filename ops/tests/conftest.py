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
    # ревью 02.11: состояние бота и журнал решений владельца — тоже во временную папку
    # (bot_state.json исключён из git, поэтому порча была невидима; на сервере — живое состояние)
    for name in ("ops.bot_state", "bot_state"):
        try:
            st = importlib.import_module(name)
        except ImportError:
            continue
        monkeypatch.setattr(st, "STATE_PATH", tmp_path / "guard_bot_state.json")
        monkeypatch.setattr(st, "DECISIONS_PATH", tmp_path / "guard_decisions.jsonl")
    # аудит 03.11: журнал доставок бота — тоже мимо рабочей копии
    for name in ("ops.bot", "bot"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "DELIVERIES_PATH"):
            monkeypatch.setattr(mod, "DELIVERIES_PATH", tmp_path / "guard_deliveries.jsonl")
    # 04.11: журнал разговоров агента диалога и журнал трат — тоже мимо рабочей копии
    for name in ("ops.bot_agent", "bot_agent"):
        try:
            ag = importlib.import_module(name)
        except ImportError:
            continue
        monkeypatch.setattr(ag, "CHAT_LOG", tmp_path / "guard_chat_agent.jsonl")
    yield
