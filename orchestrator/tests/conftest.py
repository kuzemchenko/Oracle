# -*- coding: utf-8 -*-
"""Герметичность тестов orchestrator (гигиена 01.11): heartbeat прогресса уводится
во временный файл — тестовые прогоны не трогают боевой journal/_run_progress.json
(сервер и облако делят репозиторий через git; тестовый мусор в journal/ недопустим)."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import progress as _PR    # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _progress_to_tmp(tmp_path_factory):
    # session-scope: module-фикстуры (сквозной mock-прогон воронки в test_smoke_funnel)
    # исполняются раньше function-фикстур — заглушка обязана стоять раньше их всех
    mp = pytest.MonkeyPatch()
    mp.setattr(_PR, "STATE", tmp_path_factory.mktemp("progress") / "_run_progress.json")
    yield
    mp.undo()
