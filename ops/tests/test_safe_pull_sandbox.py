# -*- coding: utf-8 -*-
"""Песочница настоящего git для охранных скриптов (аудит 03.11, P0 «охранный пакет»): голый origin,
клон «сервер» (ops/safe_pull.sh, ops/checkpoint.sh с ORACLE_DIR) и клон «облако».

Закрепляют:
  • заморозка протяжки (облако меняет настройки владельца — код 10) НЕ останавливает идеи: снимок
    reports/ideas/ из origin ложится в .cache/ideas_incoming/ (вне git, рабочая копия чистая);
  • обычная протяжка применяет идеи и убирает снимок;
  • чекпоинт при заморозке master отправляет журналы сервера в боковую ветку server-journal,
    а master в GitHub не трогает."""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("flock") and shutil.which("tar")),
                                reason="нужны git, flock, tar")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
ENV.pop("ORACLE_GIT_LOCKED", None)


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=check)


def _commit(repo, msg, files):
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    _git(repo, "push", "-q", "origin", "HEAD:master")


@pytest.fixture
def sandbox(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "ops").mkdir()
    for name in ("safe_pull.sh", "journal_guard.sh", "checkpoint.sh"):
        shutil.copy2(ROOT / "ops" / name, seed / "ops" / name)
    shutil.copy2(ROOT / ".gitignore", seed / ".gitignore")
    _git(seed, "checkout", "-q", "-b", "master")
    _commit(seed, "base", {"journal/.keep": "", "reports/ideas/.keep": "", "ops/reports/.keep": "", "config/limits.yaml": "risk: 3000\n"})
    server, cloud = tmp_path / "server", tmp_path / "cloud"
    _git(tmp_path, "clone", "-q", str(origin), str(server))
    _git(tmp_path, "clone", "-q", str(origin), str(cloud))
    return origin, server, cloud


def _run(server, script):
    return subprocess.run(["bash", str(server / "ops" / script)], cwd=server,
                          env={**ENV, "ORACLE_DIR": str(server)}, capture_output=True, text=True)


def test_freeze_still_delivers_ideas_via_snapshot(sandbox):
    origin, server, cloud = sandbox
    _commit(cloud, "идея + лимиты", {"reports/ideas/idea_2026-11-03_X.push.txt": "идея",
                                    "config/limits.yaml": "risk: 9000\n"})
    r = _run(server, "safe_pull.sh")
    assert r.returncode == 10, r.stdout + r.stderr
    snap = server / ".cache" / "ideas_incoming" / "reports" / "ideas" / "idea_2026-11-03_X.push.txt"
    assert snap.read_text("utf-8") == "идея"
    assert not (server / "reports" / "ideas" / "idea_2026-11-03_X.push.txt").exists()
    assert (server / "config" / "limits.yaml").read_text() == "risk: 3000\n"     # лимиты не протянуты
    assert "config/limits.yaml" in (server / "journal" / "pull_refused.txt").read_text()
    assert _git(server, "status", "--porcelain").stdout.strip() == ""            # рабочая копия чистая


def test_normal_pull_applies_ideas_and_drops_snapshot(sandbox):
    origin, server, cloud = sandbox
    (server / ".cache" / "ideas_incoming").mkdir(parents=True)
    _commit(cloud, "идея", {"reports/ideas/idea_2026-11-03_Y.push.txt": "идея Y"})
    r = _run(server, "safe_pull.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (server / "reports" / "ideas" / "idea_2026-11-03_Y.push.txt").exists()
    assert not (server / ".cache" / "ideas_incoming").exists()


def test_checkpoint_in_freeze_pushes_journals_to_side_branch(sandbox):
    origin, server, cloud = sandbox
    _commit(cloud, "лимиты", {"config/limits.yaml": "risk: 9000\n"})
    (server / "journal" / "x.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    r = _run(server, "checkpoint.sh")
    assert r.returncode == 7, r.stdout + r.stderr
    side = _git(origin, "show", "server-journal:journal/x.jsonl", check=False)
    assert side.returncode == 0 and '"a":1' in side.stdout
    assert _git(origin, "show", "master:journal/x.jsonl", check=False).returncode != 0   # master не тронут



def test_checkpoint_journal_touch_is_code_9_not_7(sandbox):
    """Ре-гейт 03.11 (П16): «облако тронуло journal/» — код 9 (бот: вручную НЕ протягивать), а не 7."""
    origin, server, cloud = sandbox
    _commit(cloud, "облако тронуло журнал", {"journal/x.jsonl": "{}\n"})
    (server / "journal" / "y.jsonl").write_text('{"b":2}\n', encoding="utf-8")
    r = _run(server, "checkpoint.sh")
    assert r.returncode == 9, r.stdout + r.stderr
    assert _git(origin, "show", "server-journal:journal/y.jsonl", check=False).returncode == 0
