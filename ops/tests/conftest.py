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
        # ИНЦИДЕНТ 13.11: test_battle_journal_blocked_in_cloud передаёт ЯВНЫЙ боевой путь и ждёт отказа
        # «только на сервере»; на самом сервере (/home/oracle/…) отказа нет — тест запечатал фальшивую
        # EQT-запись в настоящий journal/predictions.jsonl. Под тестами боевой журнал закрыт ВСЕГДА,
        # где бы ни шёл прогон (облако или сервер); временные пути — свободно.
        _orig_allowed = mod._battle_write_allowed

        def _deny_battle(target, _mod=mod, _orig=_orig_allowed):
            try:
                if pathlib.Path(target).resolve() == _mod.BATTLE_PRED.resolve():
                    return False
            except OSError:
                pass
            return _orig(target)
        monkeypatch.setattr(mod, "_battle_write_allowed", _deny_battle)
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
        # 11.11: модуль бота мог ещё не быть импортирован этим тестом, а импортироваться позже внутри него —
        # тогда путь доставок оставался боевым (тест дописал строку в журнал облачной копии). Импортируем заранее.
        try:
            mod = importlib.import_module(name)
        except Exception:                                    # noqa: BLE001
            mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "DELIVERIES_PATH"):
            monkeypatch.setattr(mod, "DELIVERIES_PATH", tmp_path / "guard_deliveries.jsonl")
        if mod is not None and hasattr(mod, "POINT_VERDICTS_PATH"):
            monkeypatch.setattr(mod, "POINT_VERDICTS_PATH", tmp_path / "guard_point_verdicts.jsonl")
    # 04.11: журнал разговоров агента диалога и журнал трат — тоже мимо рабочей копии
    for name in ("ops.bot_agent", "bot_agent"):
        try:
            ag = importlib.import_module(name)
        except ImportError:
            continue
        monkeypatch.setattr(ag, "CHAT_LOG", tmp_path / "guard_chat_agent.jsonl")
        monkeypatch.setattr(ag, "CHAT_REQUESTS", tmp_path / "guard_chat_requests.jsonl")
    # VQ-4 (09.11): актуальные статусы идей и очереди поиска — тоже мимо рабочей копии
    for name in ("ops.idea_revision", "idea_revision"):
        try:
            ir = importlib.import_module(name)
        except ImportError:
            continue
        monkeypatch.setattr(ir, "REV_DIR", tmp_path / "guard_revisions")
    for name in ("ops.search_queue", "search_queue"):
        try:
            sq = importlib.import_module(name)
        except ImportError:
            continue
        monkeypatch.setattr(sq, "SEARCH_DIR", tmp_path / "guard_searches")
    # ИНЦИДЕНТ 22.11: после включения флага idea_verdict_push тест агента (поиск «медь», SCHN) записал свои
    # вердикты в настоящий reports/ideas/verdicts/verdicts.jsonl — по этому файлу считается счёт метода комитета.
    # Запись вердиктов и источники карты секторов (база, EODHD) под тестами — только временные/пустые;
    # тесты, которым нужны свои источники, подставляют их сами (их подмена идёт после этой).
    for name in ("ops.idea_verdict", "idea_verdict"):
        try:
            iv = importlib.import_module(name)
        except Exception:                                    # noqa: BLE001
            continue
        monkeypatch.setattr(iv, "OUT", tmp_path / "guard_verdicts")
        monkeypatch.setattr(iv, "DAILY_SEARCHES", tmp_path / "guard_searches_daily")
    for name in ("ops.sector_view", "sector_view"):
        try:
            sv = importlib.import_module(name)
        except Exception:                                    # noqa: BLE001
            continue
        monkeypatch.setattr(sv, "FETCH", {"closes": lambda *a, **k: None, "general": lambda *a, **k: None})
        monkeypatch.setattr(sv, "CACHE", tmp_path / "guard_ticker_sector.json")
    # 22.11: ежедневная самостоятельная работа — её итоги, поиски, разговор и сообщения владельцу под тестами временные
    for name in ("ops.daily_committee", "daily_committee"):
        try:
            dc = importlib.import_module(name)
        except Exception:                                    # noqa: BLE001
            continue
        monkeypatch.setattr(dc, "OUT", tmp_path / "guard_daily")
        monkeypatch.setattr(dc, "SEARCH_ROOT", tmp_path / "guard_daily_searches")
        monkeypatch.setattr(dc, "CHAT", tmp_path / "guard_daily_chat.jsonl")
        monkeypatch.setattr(dc, "NOTICES", tmp_path / "guard_daily_notices.jsonl")
    yield
