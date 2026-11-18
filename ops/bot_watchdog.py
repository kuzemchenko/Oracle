# -*- coding: utf-8 -*-
"""ops/bot_watchdog.py — сторож бота ВНЕ бота (Q-сторож-бота, испытатель 17.11).

ПОЧЕМУ: 14.11 10:51 → 16.11 00:00 бот молчал ~37 ч, а серверный крон жил. Все алерты идут через бота
(journal/alerts.jsonl, notices.jsonl → бот), поэтому при мёртвом боте владелец не узнаёт ничего.

ЧТО: крон раз в час (без бота, без LLM) проверяет три вещи и при новой беде шлёт ОДНУ строку в Telegram
напрямую (Bot API, токен и чат из окружения .env — в вывод/журналы не пишутся):
  1) служба oracle-bot не active (systemctl is-active; нет systemctl — проверка пропускается);
  2) бот не обновлял своё состояние journal/bot_state.json дольше STALE_HOURS (бот пишет его каждый
     круг опроса, ~25 с; зависание или PermissionError при записи — тоже сюда);
  3) в рабочих папках есть файлы не от пользователя, под которым идёт крон (после ручных команд от root:
     бот и крон не могут их писать — гипотеза испытателя о PermissionError 15.11).
Одна строка на новую беду, напоминание раз в REMIND_HOURS, одна строка «снова работает» при выздоровлении.
Состояние сторожа — .cache/bot_watchdog.json; что реально ушло — строкой в journal/deliveries.jsonl.

ФЛАГ: bot_watchdog_push в config/presentation.yaml (правило 01.11: меняет приходящие владельцу сообщения →
ручной прогон → «ок» → флаг). Без флага — только вывод в journal/cron.log («послал бы: …»).

  python3 ops/bot_watchdog.py            # проверка (шлёт только при флаге)
  python3 ops/bot_watchdog.py --dry      # показать, что послал бы, ничего не трогая
"""
import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "journal" / "bot_state.json"
WATCH_STATE = ROOT / ".cache" / "bot_watchdog.json"
DELIVERIES = ROOT / "journal" / "deliveries.jsonl"
SCAN_DIRS = ("journal", "reports", "storage", ".cache", "knowledge", "config")
SERVICE = "oracle-bot"
STALE_HOURS = 2.0
REMIND_HOURS = 24.0
MAX_EXAMPLES = 3


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def service_state(run=None):
    """→ 'active' | 'inactive' | 'failed' | … | None (systemctl нет — не проверяем)."""
    run = run or subprocess.run
    try:
        r = run(["systemctl", "is-active", SERVICE], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or "").strip() or None


def foreign_files(root=ROOT, uid=None, dirs=SCAN_DIRS):
    """Файлы/папки в рабочих каталогах, чей владелец — не uid (по умолчанию — тот, кто запущен). → [относ. пути]."""
    uid = os.getuid() if uid is None else uid
    out = []
    for d in dirs:
        base = pathlib.Path(root) / d
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in (".git", "__pycache__")]
            for name in [None] + filenames:
                p = pathlib.Path(dirpath) if name is None else pathlib.Path(dirpath) / name
                try:
                    if p.lstat().st_uid != uid:
                        out.append(str(p.relative_to(root)))
                except OSError:
                    continue
    return sorted(out)


def check(now=None, state_file=STATE_FILE, root=ROOT, svc=None, foreign=None):
    """→ список бед [{"код", "текст"}] (пустой — всё хорошо). Ничего не пишет."""
    now = now or _now()
    problems = []
    if svc not in (None, "active"):
        problems.append({"код": "служба", "текст": f"служба бота не работает (состояние «{svc}»)"})
    try:
        mt = datetime.datetime.fromtimestamp(pathlib.Path(state_file).stat().st_mtime, datetime.timezone.utc)
        age_h = (now - mt).total_seconds() / 3600
        if age_h > STALE_HOURS:
            problems.append({"код": "тишина", "текст": f"бот не подаёт признаков жизни {age_h:.0f} ч "
                                                        f"(последний раз {mt:%d.%m %H:%M} UTC)"})
    except OSError:
        problems.append({"код": "тишина", "текст": "у бота нет файла состояния — он, похоже, не запускался"})
    if foreign:
        ex = ", ".join(foreign[:MAX_EXAMPLES]) + (" …" if len(foreign) > MAX_EXAMPLES else "")
        problems.append({"код": "владелец_файлов",
                         "текст": f"{len(foreign)} файл(ов) на сервере записаны не от пользователя oracle "
                                  f"(бот и крон их не перепишут): {ex}"})
    return problems


def fix_hint(problems):
    codes = {p["код"] for p in problems}
    hints = []
    if "владелец_файлов" in codes:
        hints.append("sudo chown -R oracle:oracle /home/oracle/oracle")
    if codes & {"служба", "тишина"}:
        hints.append("sudo systemctl restart oracle-bot")
    return " && ".join(hints)


def message(problems, *, recovered_since=None):
    if not problems:
        return f"Сторож Оракула: бот снова работает (беда была с {recovered_since})." if recovered_since else None
    lines = "; ".join(p["текст"] for p in problems)
    return (f"⚠ Сторож Оракула (пишет мимо бота): {lines}. Пока это не починено, сообщения и алерты "
            f"не доходят. Починка одной командой на сервере: {fix_hint(problems)}")


def decide(problems, prev, now=None):
    """Что слать с учётом прошлого состояния сторожа. → (текст|None, новое_состояние)."""
    now = now or _now()
    prev = prev or {}
    sig = sorted(p["код"] for p in problems)
    if not problems:
        if prev.get("sig"):
            return message([], recovered_since=prev.get("since")), {}
        return None, {}
    st = {"sig": sig, "since": prev.get("since") if prev.get("sig") else f"{now:%d.%m %H:%M} UTC",
          "sent": prev.get("sent")}
    last = None
    if prev.get("sent"):
        try:
            last = datetime.datetime.fromisoformat(prev["sent"])
        except ValueError:
            last = None
    due = (sig != prev.get("sig")) or last is None or (now - last).total_seconds() / 3600 >= REMIND_HOURS
    if not due:
        return None, st
    st["sent"] = now.isoformat(timespec="seconds")
    return message(problems), st


def push_enabled(root=ROOT):
    try:
        import yaml
        cfg = yaml.safe_load((pathlib.Path(root) / "config" / "presentation.yaml").read_text(encoding="utf-8")) or {}
    except Exception:                                            # noqa: BLE001 — сторож не падает
        return False
    return bool(cfg.get("bot_watchdog_push"))


def send(text, post=None):
    """Bot API напрямую. Токен — из окружения, наружу не выводится. → (ok, причина)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False, "нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в окружении"
    if post is None:
        import requests
        post = requests.post
    try:
        r = post(f"https://api.telegram.org/bot{token}/sendMessage",
                 data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=30)
        return (r.status_code == 200), f"код {r.status_code}"
    except Exception as e:                                       # noqa: BLE001
        return False, type(e).__name__                            # без repr: в нём может быть URL с токеном


def _load(p):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(*, dry=False, now=None, root=ROOT, state_file=None, watch_state=None, deliveries=None,
        svc_fn=None, foreign_fn=None, post=None, enabled=None):
    now = now or _now()
    state_file = state_file or STATE_FILE
    watch_state = pathlib.Path(watch_state or WATCH_STATE)
    deliveries = pathlib.Path(deliveries or DELIVERIES)
    svc = (svc_fn or service_state)()
    foreign = (foreign_fn or (lambda: foreign_files(root)))()
    problems = check(now, state_file, root, svc, foreign)
    text, new_state = decide(problems, _load(watch_state), now)
    ts = now.isoformat(timespec="seconds")
    if not text:
        print(f"{ts} bot_watchdog: {'беды: ' + ', '.join(p['код'] for p in problems) if problems else 'всё в порядке'}"
              " — слать нечего")
    elif dry:
        print(f"{ts} bot_watchdog [dry]: послал бы: {text}")
        return 0
    elif not (push_enabled(root) if enabled is None else enabled):
        print(f"{ts} bot_watchdog: флаг bot_watchdog_push выключен — послал бы: {text}")
        new_state["sent"] = _load(watch_state).get("sent")         # не ушло — при включении флага уйдёт сразу
    else:
        ok, why = send(text, post)
        print(f"{ts} bot_watchdog: отправка {'ушла' if ok else 'не ушла'} ({why})")
        if not ok:
            new_state["sent"] = _load(watch_state).get("sent")     # не ушло — повторим в следующий час
        else:
            import hashlib
            try:
                with open(deliveries, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": ts, "kind": "сторож бота", "ref": ",".join(new_state.get("sig") or [])
                                        or "выздоровел", "sha256": hashlib.sha256(text.encode()).hexdigest(),
                                        "chars": len(text), "buttons": False, "lint_ok": None,
                                        "message_id": None}, ensure_ascii=False) + "\n")
            except OSError as e:
                print(f"{ts} bot_watchdog: журнал доставок не записан ({type(e).__name__})")
    if not dry:
        try:
            watch_state.parent.mkdir(parents=True, exist_ok=True)
            watch_state.write_text(json.dumps(new_state, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"{ts} bot_watchdog: состояние сторожа не записано ({type(e).__name__})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    return run(dry=a.dry)


if __name__ == "__main__":
    sys.exit(main())
