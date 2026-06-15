# -*- coding: utf-8 -*-
"""Смоук event-first контура end-to-end в mock (orchestrator/event_first.py, Этап 6).

Доказывает сшивку: открытый скан → источники → полный контур (mock агенты+суд) + каскад-резолв.
Пропускается без storage/oracle.db. mock не ходит в сеть и не пишет в costs (бюджет §30).
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_first as EF    # noqa: E402
from mathlib import sealing as SEAL           # noqa: E402


@pytest.mark.skipif(not EF.DB.exists(), reason="нет storage/oracle.db")
def test_event_first_mock_wiring():
    p = EF.run_event_first(mode="mock", k=2, write=False)
    assert p["mode"] == "mock"
    assert "скан" in p and "по_источникам" in p
    # каждая запечатываемая каскад-спека §9-разрешима
    for s in p["по_источникам"]:
        cr = s.get("каскад_резолв")
        if cr:
            for sp in cr["запечатываемо"]:
                assert SEAL.is_resolvable(sp["prediction"])
