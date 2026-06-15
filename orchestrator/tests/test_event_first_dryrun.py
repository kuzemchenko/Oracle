# -*- coding: utf-8 -*-
"""Смоук сухого прогона event-first конвейера (orchestrator/event_first_dryrun.py).

Проверяет, что конвейер скан→каскад→резолв проходит на живой БД и даёт валидную структуру
протокола (mode='mock', §9-спеки разрешимы). Пропускается без storage/oracle.db.
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_first_dryrun as DR    # noqa: E402
from mathlib import sealing as SEAL                   # noqa: E402


@pytest.mark.skipif(not DR.DB.exists(), reason="нет storage/oracle.db")
def test_dryrun_structure_and_valid_specs():
    p = DR.dry_run(write=False)
    assert p["mode"] == "mock"                         # бот не пушит
    assert "скан" in p and "каскады" in p
    assert p["скан"]["сырых_сигналов"] >= 0
    # все «запечатываемо» спеки обязаны быть §9-разрешимы
    for c in p["каскады"]:
        for sp in c["резолв"]["запечатываемо"]:
            assert SEAL.is_resolvable(sp["prediction"])
            assert sp["prediction"]["direction"] in ("above", "below")
