#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/bot.py — бот-пульт «Оракула» (раздел «Интерфейс»).

Канал человеко-решения (П12, §12 «защита пользователя от себя», §15 наблюдаемость):
  • ПУШ отчётов прогона (13 полей §8) с кнопками Принять / Отклонить / Отложить;
  • кнопка «Принять» РАЗБЛОКИРУЕТСЯ через 24 ч от выдачи идеи — пауза пре-коммитмента §12
    для «медленных» (каскадных) идей; «Отклонить»/«Отложить» доступны сразу;
  • решения пишутся в journal/decisions_user.jsonl (принял/отклонил/отложил + мотив, §12);
  • утренняя строка бюджета §15 и АЛЕРТЫ бюджета (потолки config/limits.yaml, инвариант 5);
  • АЛЕРТЫ срабатывания структурных триггеров листа ожидания (§17, агент своевременности).

Транспорт — лёгкий long-polling на requests (без тяжёлого async-стека: проект ценит
«минимум источников/воспроизводимость»). Запускается как systemd-сервис (ops/oracle-bot.service),
переживает перезагрузки. Окружение (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ключи) поднимается из
.env, как в cron.

ЗАЩИТА: бот реагирует ТОЛЬКО на TELEGRAM_CHAT_ID (allow-list) — кнопки решений не должны быть
доступны посторонним. При первом запуске, если chat_id не задан, бот подсказывает свой chat_id.
"""
import os
import sys
import json
import time
import argparse
import datetime
import pathlib

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # плоские ops-модули
import bot_state as S          # noqa: E402
import bot_reports as R        # noqa: E402
import bot_watchlist as W      # noqa: E402
import budget as B             # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG_TAG = "[bot]"

POLL_TIMEOUT = 25              # long-poll getUpdates, сек
HTTP_TIMEOUT = POLL_TIMEOUT + 15
BUDGET_HOUR = int(os.environ.get("BOT_BUDGET_HOUR", "7"))   # час утренней строки (UTC), как cron 07:00
IDLE_NO_TOKEN = 30             # сон, если токен не задан (без crash-loop)


def log(*a):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(ts, LOG_TAG, *a, flush=True)


# ── тонкий Telegram-клиент ──────────────────────────────────────────────────────────
class Telegram:
    def __init__(self, token, session=None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.s = session or requests.Session()

    def _call(self, method, params=None, timeout=HTTP_TIMEOUT):
        try:
            r = self.s.post(f"{self.base}/{method}", json=params or {}, timeout=timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log("telegram ошибка", method, repr(e))
            return None
        if not data.get("ok"):
            log("telegram !ok", method, data.get("description"))
            return None
        return data.get("result")

    def get_updates(self, offset, timeout=POLL_TIMEOUT):
        return self._call("getUpdates",
                          {"offset": offset, "timeout": timeout,
                           "allowed_updates": ["message", "callback_query"]},
                          timeout=timeout + 15) or []

    def send_message(self, chat_id, text, reply_markup=None):
        p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            p["reply_markup"] = reply_markup
        res = self._call("sendMessage", p)
        return (res or {}).get("message_id")

    def edit_reply_markup(self, chat_id, message_id, reply_markup):
        return self._call("editMessageReplyMarkup",
                          {"chat_id": chat_id, "message_id": message_id,
                           "reply_markup": reply_markup})

    def answer_callback(self, cb_id, text=None, show_alert=False):
        p = {"callback_query_id": cb_id, "show_alert": show_alert}
        if text:
            p["text"] = text[:200]
        return self._call("answerCallbackQuery", p)

    def send_chat_action(self, chat_id, action="typing"):
        return self._call("sendChatAction", {"chat_id": chat_id, "action": action})


# ── сервис ──────────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self, tg, chat_id, state=None):
        self.tg = tg
        self.chat_id = str(chat_id) if chat_id else None
        self.state = state if state is not None else S.load_state()

    def save(self):
        S.save_state(self.state)

    def _allowed(self, incoming_chat_id):
        return self.chat_id is not None and str(incoming_chat_id) == self.chat_id

    # ── входящие апдейты ────────────────────────────────────────────────────────────
    def handle_update(self, upd):
        self.state["update_offset"] = upd["update_id"] + 1
        if "callback_query" in upd:
            self._on_callback(upd["callback_query"])
        elif "message" in upd:
            self._on_message(upd["message"])

    def _on_callback(self, cq):
        cb_id = cq.get("id")
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if not self._allowed(chat):
            self.tg.answer_callback(cb_id, "Нет доступа.")
            return
        data = cq.get("data") or ""
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "d":
            self.tg.answer_callback(cb_id)        # noop / неизвестная кнопка
            return
        _, code, token = parts
        action = {"a": "accept", "r": "reject", "p": "defer"}.get(code)
        card = self.state["pending"].get(token)
        if not action or not card:
            self.tg.answer_callback(cb_id, "Идея не найдена или устарела.")
            return
        if card.get("status") and card["status"] != "pending":
            self.tg.answer_callback(cb_id, f"Уже {S.action_ru(card['status'])}.")
            return

        issued_at = card.get("issued_at")
        # Пауза пре-коммитмента §12: «Принять» — только после 24 ч.
        if action == "accept" and not S.accept_unlocked(issued_at):
            rem = S.hours_remaining(issued_at) or 0.0
            unlock = S.accept_unlock_at(issued_at)
            self.tg.answer_callback(
                cb_id,
                f"Пауза пре-коммитмента §12: «Принять» откроется через {rem:.0f} ч "
                f"(в {S.iso(unlock) if unlock else '—'}). Это защита от импульсивного входа. "
                f"Можно «Отклонить»/«Отложить».",
                show_alert=True)
            return

        # Запись решения (append-only журнал §12).
        S.append_decision(run_id=card["run_id"], asset=card["asset"],
                          direction=card.get("direction"), score=card.get("score"),
                          action=action, issued_at=issued_at, chat_id=self.chat_id)
        card["status"] = action
        # Снимаем клавиатуру с карточки и просим мотив.
        if msg.get("message_id"):
            self.tg.edit_reply_markup(chat, msg["message_id"], {"inline_keyboard": []})
        self.tg.answer_callback(cb_id, f"Записано: {S.action_ru(action)}.")
        # Слот ожидания мотива (один — на последнее решение; §12 «мотив»).
        self.state["awaiting_motive"] = {"token": token, "run_id": card["run_id"],
                                         "asset": card["asset"], "action": action}
        self.tg.send_message(
            chat,
            f"{S.action_ru(action)}: {card['asset']}. Укажи МОТИВ решения ответным сообщением "
            f"(§12 — сверим с исходом, покажу твою калибровку). Или пришли «-», чтобы пропустить.")
        self.save()

    def _on_message(self, msg):
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not self._allowed(chat):
            # Онбординг: подсказать chat_id, если allow-list ещё не настроен.
            if self.chat_id is None and chat is not None:
                self.tg.send_message(chat, f"Твой chat_id: {chat}. Пропиши в .env "
                                           f"TELEGRAM_CHAT_ID={chat} и перезапусти сервис.")
            return
        if not text:
            return
        # Ожидаем мотив к последнему решению?
        slot = self.state.get("awaiting_motive")
        if slot and not text.startswith("/"):
            if text != "-":
                S.append_motive(run_id=slot["run_id"], asset=slot["asset"],
                                action=slot["action"], motive=text, chat_id=self.chat_id)
                self.tg.send_message(chat, "Мотив записан (§12).")
            else:
                self.tg.send_message(chat, "Мотив пропущен.")
            self.state["awaiting_motive"] = None
            self.save()
            return
        if text.startswith("/"):
            self._command(chat, text)
            return
        # Свободный текст → диалог с Дирижёром (как в терминале).
        self._chat(chat, text)

    def _command(self, chat, text):
        cmd = text.split()[0].lstrip("/").lower()
        if cmd in ("start", "help"):
            self.tg.send_message(chat,
                "Пульт «Оракула». Пуш отчётов с кнопками Принять/Отклонить/Отложить "
                "(Принять — через 24 ч, пауза §12).\n\n"
                "💬 Просто напиши мне вопрос обычным текстом — отвечу тем же мозгом, что ведёт "
                "проект в терминале (Дирижёр). Например: «как ты оцениваешь идею по меди?», "
                "«что у нас с бюджетом?», «объясни, как работает воронка».\n\n"
                "Команды: /status — открытые идеи; /budget — строка бюджета; "
                "/watchlist — лист ожидания; /reset — очистить контекст диалога.")
        elif cmd == "reset":
            self.state["chat_history"] = []
            self.save()
            self.tg.send_message(chat, "Контекст диалога очищен.")
        elif cmd == "status":
            pend = [c for c in self.state["pending"].values() if c.get("status") == "pending"]
            if not pend:
                self.tg.send_message(chat, "Открытых идей на решении нет.")
            else:
                lines = ["Открытые идеи:"]
                for c in pend:
                    lock = ("готова к приёму" if S.accept_unlocked(c["issued_at"])
                            else f"приём через {S.hours_remaining(c['issued_at']):.0f}ч")
                    lines.append(f"• {c['asset']} ({c['run_id']}) — {lock}")
                self.tg.send_message(chat, "\n".join(lines))
        elif cmd == "budget":
            self.tg.send_message(chat, R.format_budget_line(self._budget_one_liner(with_key=True)))
        elif cmd == "watchlist":
            entries = W.current_entries()
            if not entries:
                self.tg.send_message(chat, "Лист ожидания пуст.")
            else:
                lines = ["Лист ожидания:"]
                for e in entries.values():
                    t = e.get("trigger")
                    cond = f"{t['symbol']} {t['dir']} {t['level']}" if t else "ручная проверка"
                    lines.append(f"• {e.get('asset')} — {cond}")
                self.tg.send_message(chat, "\n".join(lines))
        else:
            self.tg.send_message(chat, "Неизвестная команда. /help")

    # ── свободный диалог с Дирижёром ─────────────────────────────────────────────────
    def _send_long(self, chat, text):
        """Длинный ответ режем на части под лимит Telegram (4096)."""
        import bot_chat as C
        if not text:
            return
        for i in range(0, len(text), C.MAX_REPLY_CHUNK):
            self.tg.send_message(chat, text[i:i + C.MAX_REPLY_CHUNK])

    def _chat(self, chat, text):
        """Вопрос пользователя → ответ Дирижёра (роль conductor). Уважает бюджет §11 и П8."""
        # Денежные ворота §11: при превышении месячного потолка не жжём токены на диалог.
        try:
            if self._budget_status().get("exit_code") == 3:
                self.tg.send_message(
                    chat, "Месячный потолок бюджета токенов исчерпан (§11) — диалог с моделью "
                          "приостановлен до нового месяца/пополнения. Команды /status, /budget, "
                          "/watchlist работают.")
                return
        except Exception as e:                                   # noqa: BLE001
            log("chat budget check ошибка", repr(e))
        if hasattr(self.tg, "send_chat_action"):
            self.tg.send_chat_action(chat, "typing")
        import bot_chat as C
        hist = self.state.setdefault("chat_history", [])
        try:
            reply, _cost = C.answer(text, history=hist)
        except Exception as e:                                   # noqa: BLE001
            log("chat ошибка", repr(e))
            self.tg.send_message(
                chat, "Не смог ответить (модель недоступна или ключ OpenRouter не задан). "
                      "Попробуй позже или задай вопрос иначе.")
            return
        if not reply:
            self.tg.send_message(chat, "Пустой ответ модели — попробуй переформулировать.")
            return
        hist.append({"role": "user", "text": text})
        hist.append({"role": "assistant", "text": reply})
        self.state["chat_history"] = hist[-2 * C.MAX_HISTORY_TURNS:]
        self._send_long(chat, reply)
        self.save()
        log("диалог: ответ отправлен,", len(reply), "симв")

    # ── бюджет ────────────────────────────────────────────────────────────────────
    def _budget_status(self):
        limits = B.load_budget_limits()
        tokens, _by_mode, _by_model = B.oracle_monthly_spend(limits["costs_log"])
        return B.compute_status(tokens, limits)

    def _budget_one_liner(self, with_key=False):
        limits = B.load_budget_limits()
        tokens, _bm, _bmod = B.oracle_monthly_spend(limits["costs_log"])
        st = B.compute_status(tokens, limits)
        key = B.key_reference() if with_key else {"error": "не запрашивалось"}
        return B.one_liner(st, key)

    # ── периодические задания ───────────────────────────────────────────────────────
    def tick(self, now=None):
        now = now or S.now_utc()
        try:
            self._tick_reports()
        except Exception as e:                                   # noqa: BLE001
            log("tick reports ошибка", repr(e))
        try:
            self._tick_watchlist()
        except Exception as e:                                   # noqa: BLE001
            log("tick watchlist ошибка", repr(e))
        try:
            self._tick_budget(now)
        except Exception as e:                                   # noqa: BLE001
            log("tick budget ошибка", repr(e))
        self.save()

    def _tick_reports(self):
        for proto in R.new_runs(self.state["pushed_runs"]):
            run_id = proto.get("run_id")
            issued_at = proto.get("ts")
            ideas = R.ideas_from_protocol(proto)
            if ideas:
                for idea in ideas:
                    asset = idea.get("актив")
                    token = S.idea_token(run_id, asset)
                    text = R.format_report(proto, idea)
                    kb = R.build_keyboard(token, issued_at)
                    mid = self.tg.send_message(self.chat_id, text, reply_markup=kb)
                    self.state["pending"][token] = {
                        "run_id": run_id, "asset": asset,
                        "direction": idea.get("направление"), "score": idea.get("балл"),
                        "issued_at": issued_at, "message_id": mid, "status": "pending",
                    }
                    log("пуш идеи", run_id, asset, "token", token)
            else:
                self.tg.send_message(self.chat_id, R.format_weak_day(proto))
                log("пуш слабого дня", run_id)
            # РАНО-идеи прогона → лист ожидания (manual_check; авто-триггер привяжет оператор).
            added = W.ingest_protocol(proto, existing_ids=self.state["seen_watchlist"])
            for rec in added:
                self.state["seen_watchlist"].append(rec["id"])
            self.state["pushed_runs"].append(run_id)

    def _tick_watchlist(self):
        for fired in W.evaluate(already_fired=self.state["fired_triggers"]):
            eid = fired["entry"]["id"]
            self.tg.send_message(self.chat_id, R.format_trigger_alert(fired))
            W.fire_entry(fired["entry"], fired["observed"])
            self.state["fired_triggers"].append(eid)
            log("алерт триггера", eid, fired["entry"].get("asset"))

    def _tick_budget(self, now):
        today = now.date().isoformat()
        # Утренняя строка §15 — раз в сутки после BUDGET_HOUR.
        if now.hour >= BUDGET_HOUR and self.state.get("last_budget_line_date") != today:
            self.tg.send_message(self.chat_id, R.format_budget_line(self._budget_one_liner(with_key=True)))
            self.state["last_budget_line_date"] = today
            log("утренняя строка бюджета", today)
        # Алерт на потолки (только локальный спенд, без сети) — дедуп в сутки.
        st = self._budget_status()
        if st["status"] != "OK" and self.state.get("last_budget_alert_date") != today:
            ol = self._budget_one_liner(with_key=False)
            self.tg.send_message(self.chat_id, R.format_budget_alert(st, ol))
            self.state["last_budget_alert_date"] = today
            log("алерт бюджета", st["status"])

    # ── baseline против спама бэклогом при первом запуске ────────────────────────────
    def initialize_baseline(self):
        """Первый запуск на машине с историей: помечаем все существующие протоколы и записи
        watchlist как уже виденные, БЕЗ пуша — иначе бот вывалит весь архив. Пушим только новое."""
        if self.state.get("initialized"):
            return
        for proto in R.scan_protocols():
            rid = proto.get("run_id")
            if rid and rid not in self.state["pushed_runs"]:
                self.state["pushed_runs"].append(rid)
        for eid in W.current_entries().keys():
            if eid not in self.state["seen_watchlist"]:
                self.state["seen_watchlist"].append(eid)
        # Слить отложенные апдейты Telegram (offset → последний), чтобы не отвечать на старьё.
        try:
            ups = self.tg.get_updates(self.state.get("update_offset", 0), timeout=0)
            if ups:
                self.state["update_offset"] = ups[-1]["update_id"] + 1
        except Exception as e:                                   # noqa: BLE001
            log("baseline drain ошибка", repr(e))
        self.state["initialized"] = True
        self.save()
        log("baseline инициализирован: протоколов", len(self.state["pushed_runs"]),
            "watchlist", len(self.state["seen_watchlist"]))

    # ── главная петля ────────────────────────────────────────────────────────────────
    def run_forever(self):
        log("старт. chat_id", self.chat_id, "BUDGET_HOUR", BUDGET_HOUR)
        self.initialize_baseline()
        if self.chat_id:
            self.tg.send_message(self.chat_id, "🤖 Пульт «Оракула» на связи. Пиши вопросы текстом — "
                                               "отвечу как Дирижёр. /help")
        while True:
            try:
                ups = self.tg.get_updates(self.state.get("update_offset", 0))
                for upd in ups:
                    self.handle_update(upd)
                if ups:
                    self.save()
                self.tick()
            except Exception as e:                               # noqa: BLE001
                log("петля ошибка", repr(e))
                time.sleep(3)


# ── точка входа ──────────────────────────────────────────────────────────────────────
def selfcheck():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    print("=== bot --check ===")
    print("TELEGRAM_BOT_TOKEN:", "задан" if token else "НЕ ЗАДАН (нужно в .env)")
    print("TELEGRAM_CHAT_ID:", chat or "НЕ ЗАДАН (узнается при первом сообщении боту)")
    try:
        limits = B.load_budget_limits()
        tokens, _bm, _bmod = B.oracle_monthly_spend(limits["costs_log"])
        st = B.compute_status(tokens, limits)
        print("бюджет: OK, статус", st["status"], f"(токены ${st['oracle_tokens_usd']:.2f}/${st['tokens_cap']:.0f})")
    except Exception as e:                                       # noqa: BLE001
        print("бюджет: ОШИБКА", repr(e))
    protos = R.scan_protocols()
    with_ideas = sum(1 for p in protos if R.ideas_from_protocol(p))
    print(f"протоколов воронки: {len(protos)} (с идеями: {with_ideas})")
    print("записей листа ожидания (armed):", len(W.current_entries()))
    db_ok = (ROOT / "storage" / "oracle.db").exists()
    print("oracle.db:", "есть" if db_ok else "НЕТ")
    st_obj = S.load_state()
    print("bot_state:", "инициализирован" if st_obj.get("initialized") else "свежий",
          "· pending", len(st_obj.get("pending", {})))
    print("=== ок ===")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Бот-пульт «Оракула» (Интерфейс).")
    ap.add_argument("--check", action="store_true", help="проверка конфигурации/окружения и выход")
    args = ap.parse_args(argv)
    if args.check:
        return selfcheck()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        # Не падаем в crash-loop: ждём, пока админ добавит токен в .env и перезапустит.
        log("TELEGRAM_BOT_TOKEN не задан — пропиши в .env и перезапусти сервис. Сплю.")
        while not os.environ.get("TELEGRAM_BOT_TOKEN"):
            time.sleep(IDLE_NO_TOKEN)
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
    bot = Bot(Telegram(token), chat)
    bot.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
