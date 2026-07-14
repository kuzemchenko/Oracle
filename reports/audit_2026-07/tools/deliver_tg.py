# -*- coding: utf-8 -*-
"""Доставка аудита в Telegram тем же механизмом, что недельные отчёты (токен/chat_id из .env,
как бот). Отправляет сводку сообщением + SUMMARY_RU.md документом (sendDocument). С ретраями
(в bot.log были Bad Gateway/ReadTimeout). НЕ трогает конфиг бота, НЕ пишет в журналы."""
import os, sys, time, pathlib, requests

ROOT = pathlib.Path("/root/oracle")

def load_env():
    for line in open(ROOT / ".env"):
        line = line.strip()
        if line.startswith("export "): line = line[7:]
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def post(method, **kw):
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{tok}/{method}"
    last = None
    for attempt in range(5):
        try:
            r = requests.post(url, timeout=60, **kw)
            j = r.json()
            if j.get("ok"): return True, j
            last = j.get("description", str(j))
        except Exception as e:
            last = repr(e)
        time.sleep(3 * (attempt + 1))
    return False, last

def main():
    load_env()
    chat = os.environ["TELEGRAM_CHAT_ID"]
    summary = (ROOT / "reports/audit_2026-07/tools/tg_summary.txt").read_text(encoding="utf-8")
    doc = ROOT / "reports/audit_2026-07/SUMMARY_RU.md"

    ok1, r1 = post("sendMessage", data={"chat_id": chat, "text": summary,
                                        "disable_web_page_preview": "true"})
    print("sendMessage:", "OK" if ok1 else f"ОШИБКА: {r1}")

    ok2 = False
    if ok1:
        with open(doc, "rb") as f:
            ok2, r2 = post("sendDocument",
                           data={"chat_id": chat, "caption": "Полный отчёт аудита (SUMMARY_RU.md)"},
                           files={"document": ("SUMMARY_RU_audit_2026-07.md", f, "text/markdown")})
        print("sendDocument:", "OK" if ok2 else f"ОШИБКА: {r2}")

    sys.exit(0 if (ok1 and ok2) else 1)

if __name__ == "__main__":
    main()
