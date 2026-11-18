# -*- coding: utf-8 -*-
"""Сторож бота мимо бота (Q-сторож-бота, испытатель 17.11)."""
import datetime
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from ops import bot_watchdog as W  # noqa: E402

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)


def _state(tmp_path, hours_ago, now=NOW):
    p = tmp_path / "bot_state.json"
    p.write_text("{}")
    t = (now - datetime.timedelta(hours=hours_ago)).timestamp()
    os.utime(p, (t, t))
    return p


class _Resp:
    def __init__(self, code):
        self.status_code = code


def _run(tmp_path, hours_ago=0.1, svc="active", foreign=(), enabled=True, code=200, sent=None, now=NOW):
    sent = sent if sent is not None else []

    def post(url, data, timeout):
        sent.append((url, data))
        return _Resp(code)
    W.run(now=now, root=tmp_path, state_file=_state(tmp_path, hours_ago, now), watch_state=tmp_path / "w.json",
          deliveries=tmp_path / "d.jsonl", svc_fn=lambda: svc, foreign_fn=lambda: list(foreign), post=post,
          enabled=enabled)
    return sent


def test_healthy_bot_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert _run(tmp_path) == []
    assert not (tmp_path / "d.jsonl").exists()


def test_stale_state_alerts_once_then_reminds_daily_then_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = _run(tmp_path, hours_ago=3)
    assert len(sent) == 1 and "не подаёт признаков жизни 3 ч" in sent[0][1]["text"]
    assert "systemctl restart oracle-bot" in sent[0][1]["text"]
    _run(tmp_path, hours_ago=4, sent=sent, now=NOW + datetime.timedelta(hours=1))
    assert len(sent) == 1                                     # та же беда через час — молчим
    _run(tmp_path, hours_ago=30, sent=sent, now=NOW + datetime.timedelta(hours=25))
    assert len(sent) == 2                                     # раз в сутки — напоминание
    _run(tmp_path, hours_ago=0.01, sent=sent, now=NOW + datetime.timedelta(hours=26))
    assert len(sent) == 3 and "снова работает" in sent[2][1]["text"]
    kinds = [json.loads(l)["kind"] for l in (tmp_path / "d.jsonl").read_text().splitlines()]
    assert kinds == ["сторож бота"] * 3


def test_inactive_service_and_foreign_files(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = _run(tmp_path, svc="failed", foreign=["journal/a.jsonl", "journal/b", "c", "d"])
    t = sent[0][1]["text"]
    assert "«failed»" in t and "4 файл(ов)" in t and "…" in t
    assert t.index("chown") < t.index("restart")              # сначала права, потом рестарт


def test_new_kind_of_problem_sends_again(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = _run(tmp_path, hours_ago=3)
    _run(tmp_path, hours_ago=4, svc="inactive", sent=sent, now=NOW + datetime.timedelta(hours=1))
    assert len(sent) == 2


def test_flag_off_sends_nothing_but_sends_right_after_flag_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = _run(tmp_path, hours_ago=3, enabled=False)
    assert sent == [] and "послал бы" in capsys.readouterr().out
    _run(tmp_path, hours_ago=4, enabled=True, sent=sent, now=NOW + datetime.timedelta(hours=1))
    assert len(sent) == 1


def test_failed_send_retries_next_hour(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = _run(tmp_path, hours_ago=3, code=502)
    _run(tmp_path, hours_ago=4, sent=sent, now=NOW + datetime.timedelta(hours=1))
    assert len(sent) == 2 and not (tmp_path / "d.jsonl").read_text().count("\n") > 1


def test_token_never_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET123"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def boom(url, data, timeout):
        raise RuntimeError(url)
    W.run(now=NOW, root=tmp_path, state_file=_state(tmp_path, 3), watch_state=tmp_path / "w.json",
          deliveries=tmp_path / "d.jsonl", svc_fn=lambda: "active", foreign_fn=list, post=boom, enabled=True)
    assert "SECRET123" not in capsys.readouterr().out


def test_no_env_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert W.send("x", post=lambda *a, **k: _Resp(200)) == (False, "нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в окружении")


def test_foreign_files_scan(tmp_path):
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal" / "x.jsonl").write_text("")
    assert W.foreign_files(tmp_path) == []
    assert "journal/x.jsonl" in W.foreign_files(tmp_path, uid=os.getuid() + 1)


def test_missing_systemctl_skips_service_check():
    def run(*a, **k):
        raise FileNotFoundError
    assert W.service_state(run) is None
