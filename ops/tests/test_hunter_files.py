# -*- coding: utf-8 -*-
"""Файлы охоты пишет код (ops/hunter_files.py, перепроверка №2 03.11): тени и карточка лидера
копируются из файлов шлагбаума, а не переписываются моделью из обрезанного JSON (П8)."""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import hunter_files as HF          # noqa: E402
from test_idea_gate import CARD    # noqa: E402


def test_shadows_and_card_copied_by_code(tmp_path):
    files = []
    for i in range(6):                                  # больше, чем влезало в .slice(0, 60000)
        c = {**CARD, "ticker": f"T{i}.US", "spec9": {**CARD["spec9"], "asset": f"T{i}.US"},
             "gate_file": f"/x/{i}.json", "thesis": "длинный текст " * 900}
        p = tmp_path / f"r1_A_T{i}.json"
        p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        files.append(str(p))
    nospec = tmp_path / "nospec.json"
    nospec.write_text(json.dumps({**CARD, "spec9": {"asset": "Z.US"}}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "killed.shadow.json"
    rows, skipped = HF.write_shadows(out, files + [str(nospec), str(tmp_path / "missing.json")])
    got = json.loads(out.read_text("utf-8"))
    assert len(rows) == len(got) == 6 and all("gate_file" not in r for r in got)
    assert len(skipped) == 2
    dst = tmp_path / "idea.card.json"
    HF.write_card(files[0], dst)
    c = json.loads(dst.read_text("utf-8"))
    assert c["ticker"] == "T0.US" and "gate_file" not in c
    empty = tmp_path / "empty.shadow.json"
    assert HF.main(["shadow", str(empty)]) == 0 and json.loads(empty.read_text("utf-8")) == []


def test_journal_guard_allows_only_proposals_and_md():
    """Единое правило safe_pull/checkpoint: облаку можно journal/proposals/ и *.md в корне."""
    guard = ROOT / "ops" / "journal_guard.sh"
    sealed = "journal/" + "predictions" + ".jsonl"
    paths = ("journal/proposals/x.md\njournal/notes.md\n" + sealed + "\n"
             "journal/challenges/a.json\njournal/deep_dive.jsonl\njournal/sub/y.md\n")
    script = f'. "{guard}"; printf "%s" "$P" | grep -Ev "$JOURNAL_CLOUD_ALLOWED"'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"P": paths, "PATH": "/usr/bin:/bin"})
    assert r.stdout.split() == [sealed, "journal/challenges/a.json", "journal/deep_dive.jsonl",
                                "journal/sub/y.md"]
