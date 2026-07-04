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
import re
import sys
import json
import time
import argparse
import datetime
import pathlib
import threading

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # плоские ops-модули
import bot_state as S          # noqa: E402
import bot_reports as R        # noqa: E402
import bot_watchlist as W      # noqa: E402
import budget as B             # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # для пакета orchestrator (строка прогресса прогона §15)
try:
    from orchestrator import progress as PROG   # noqa: E402
except Exception:                                # бот не должен падать из-за индикатора
    PROG = None
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
        self._job = None            # текущая фоновая задача (прогон/калибровка/сверка)
        self._job_label = None      # её человеческое имя для /progress и сообщений

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
                f"«Беру в работу» откроется через {rem:.0f} ч (в {S.iso(unlock) if unlock else '—'}). "
                f"Это намеренная пауза-сутки — защита от решения на эмоциях. "
                f"Пока можно нажать «Мимо» или «Позже».",
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
            f"Записал: «{S.action_ru(action)}» по {card['asset']}.\n"
            f"Черкни одной фразой ПОЧЕМУ так решил (ответным сообщением) — позже сверим с тем, "
            f"что вышло, и покажу, насколько точны твои решения. Или пришли «-», чтобы пропустить.")
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
                self.tg.send_message(chat, "Записал причину, спасибо — сверим с результатом.")
            else:
                self.tg.send_message(chat, "Ок, без причины.")
            self.state["awaiting_motive"] = None
            self.save()
            return
        if text.startswith("/"):
            self._command(chat, text)
            return
        # R9c: «проверь идею <X>» → полный состязательный контур по идее пользователя.
        idea = self._intent_check_idea(text)
        if idea:
            self._cmd_check_idea(chat, idea)
            return
        # Намерение в свободной речи: «сделай прогон», «найди идеи» → боевой прогон (узкий
        # вайтлист, чтобы не перехватывать настоящие вопросы). Прочее — диалог с Дирижёром.
        if self._intent_run_funnel(text):
            self._cmd_run_funnel(chat)
            return
        # Свободный текст → диалог с Дирижёром (как в терминале).
        self._chat(chat, text)

    @staticmethod
    def _intent_run_funnel(text):
        """Грубое распознавание «запусти прогон» в свободном тексте. Узко — лучше пропустить
        намерение (тогда ответит Дирижёр и подскажет /run-funnel), чем перехватить вопрос."""
        t = text.lower().strip().strip(".!?…")
        for pre in ("да, ", "да ", "ок, ", "ок ", "окей ", "хорошо, ", "хорошо ", "давай, "):
            if t.startswith(pre):
                t = t[len(pre):].strip()
        if len(t) > 60:                       # длинная фраза — почти наверняка вопрос, не команда
            return False
        exact = {"прогон", "прогон воронки", "сделай прогон", "сделай прогон воронки",
                 "запусти прогон", "запусти воронку", "давай прогон", "новый прогон",
                 "найди идеи", "найди идею", "найди мне идеи", "поищи идеи", "ищи идеи"}
        if t in exact:
            return True
        return (t.startswith(("сделай прогон", "запусти прогон", "прогон воронки", "запусти воронку"))
                or t.startswith(("найди иде", "поищи иде", "найди мне иде")))

    @staticmethod
    def _intent_check_idea(text):
        """Намерение «прогони состязательный разбор» в свободной речи. Возвращает текст идеи (из
        него тикер достанет INTRO._ticker) либо None. Оба класса роутятся в ПОЛНЫЙ контур
        _cmd_check_idea (генератор → критик → слепой судья §8), НЕ в одно-семейный чат:
          • явная ИДЕЯ: «проверь/оцени/разбери [мою] идею <X>» (поведение прежнее, не ломаем);
          • КОМПАНИЯ/ТИКЕР: «разбери/копни/проверь/оцени [компанию|тикер|бумагу] CLF» — именно так
            дайджест приглашает копнуть: «напиши разбери ТИКЕР».
        Узко по тикеру: голые «разбери …»/«копни …» перехватываем ТОЛЬКО при тикер-образном токене
        (2–5 заглавных латинских), иначе это обычный вопрос («разбери ситуацию на рынке») — пусть
        отвечает Дирижёр. Явные «… компанию/тикер/бумагу X» роутим всегда (намерение однозначно)."""
        low = text.lower().strip()
        body = text.strip()
        # — класс 1: явная «идея» (поведение прежнее) —
        for pre in ("проверь мою идею", "оцени мою идею", "проверь идею", "оцени идею",
                    "разбери идею", "проверь идею:"):
            if low.startswith(pre):
                return body[len(pre):].lstrip(" :—-").strip() or None
        # — класс 2a: явный объект «компанию/тикер/бумагу» — намерение однозначно —
        for pre in ("разбери компанию", "разбери тикер", "разбери бумагу",
                    "оцени компанию", "оцени тикер", "оцени бумагу",
                    "проверь компанию", "проверь тикер", "проверь бумагу",
                    "копни компанию", "копни тикер", "копни бумагу"):
            if low.startswith(pre):
                return body[len(pre):].lstrip(" :—-").strip() or None
        # — класс 2б: голые «разбери/копни <ТИКЕР>» — только при тикер-образном токене —
        for pre in ("разбери", "копни"):
            if low.startswith(pre):
                rest = body[len(pre):].lstrip(" :—-").strip()
                if rest and re.search(r"\b[A-Z]{2,5}\b", rest):
                    return rest
                return None
        return None

    def _cmd_check_idea(self, chat, idea_text):
        """R9c: проверка ИДЕИ пользователя полным состязательным контуром (21 агент + слепой суд §8).
        Тикер из идеи → добор истории (B2.6) → run_funnel theme-focused → вердикт. seal НЕ делаем."""
        if self._job_busy():
            self._busy_notice(chat)
            return
        if self._budget_blocked(chat, "проверку идеи"):
            return
        if not os.environ.get("OPENROUTER_API_KEY"):
            self.tg.send_message(chat, "Нет ключа OpenRouter — проверка идеи невозможна.")
            return
        import bot_introspect as INTRO
        ticker = INTRO._ticker(idea_text)
        if not ticker:
            self.tg.send_message(chat, "Не вижу тикер в идее. Например: «проверь идею: CLF» или "
                                       "«проверь идею NUE — сталь вырастет на пошлинах».")
            return
        self.tg.send_message(
            chat, f"🔬 Проверяю идею по {ticker} полным состязательным контуром (генератор → критик → "
                  f"слепой судья, §8). Несколько минут — вердикт пришлю сюда. «Не проходит» — тоже "
                  f"честный результат.")

        def _job():
            import datetime as _dt
            import sqlite3 as _sq
            from data import eodhd as _E
            from orchestrator.funnel import run_funnel
            con = _sq.connect(str(ROOT / "storage" / "oracle.db"))
            try:
                _E.ensure_history(con, [ticker], os.environ.get("EODHD_API_KEY", ""))
            finally:
                con.close()
            rid = ("idea_" + ticker.split(".")[0] + "_"
                   + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            p = run_funnel(theme=ticker, mode="live", theme_focused=True, write=True, run_id=rid)
            ideas = (p.get("этап6_синтез") or {}).get("отчёты") or []
            log("проверка идеи завершена:", rid, "· идей:", len(ideas))
            if not ideas:                                    # карточки §8 пушит _tick_reports; здесь — «не прошло»
                self.tg.send_message(
                    self.chat_id, f"🔬 Идея по {ticker}: контур НЕ выдал прошедшую идею — слепой суд / "
                                  f"фильтры её зарезали (честный результат, не поломка). Разбор: /progress.")

        self._start_job("проверка идеи", _job)

    def _cmd_devops(self, chat):
        """§R6: последние devops-ПРЕДЛОЖЕНИЯ (анализ террейна/калибровки на подпись). Нет отчёта —
        генерим на лету (read-only). Система сама НИЧЕГО не применяет (автономия 0)."""
        import devops_loop as DV
        pdir = ROOT / "journal" / "proposals"
        mds = sorted(pdir.glob("proposals_*.md")) if pdir.exists() else []
        text = mds[-1].read_text(encoding="utf-8") if mds else DV._markdown(
            DV.run_devops_proposer(n=10, write=False))
        self.tg.send_message(chat, text[:3800] + ("…" if len(text) > 3800 else ""))

    def _command(self, chat, text):
        cmd = text.split()[0].lstrip("/").lower()
        if cmd in ("start", "help"):
            self.tg.send_message(chat,
                "Привет! Я «Оракул» — помощник по инвест-идеям. Что я делаю:\n\n"
                "• Каждый день анализирую новости и рынок и ищу НЕочевидные идеи — где рынок ещё "
                "не заметил выгоду. Если стоящего нет — честно молчу или пишу «идей нет».\n"
                "• Когда идея есть — пришлю карточку простым языком: что это, почему, чем рискуем, "
                "и кнопки «Беру в работу / Мимо / Позже».\n"
                "• Я НЕ даю инвест-рекомендаций и не торгую — последнее слово и деньги всегда за тобой.\n\n"
                "💬 Можешь просто писать мне вопросы обычным текстом — отвечу по-человечески. "
                "Например: «объясни последнюю идею», «что сейчас происходит?», «почему ты выбрал медь?».\n\n"
                "🚀 Можешь сам запустить прогон: напиши «/run-funnel» (или просто «сделай прогон» / "
                "«найди идеи») — я прогоню воронку (открытый скан мира → каскады в компании) и пришлю "
                "дайджест. Прогон идёт несколько минут; ход — в /progress. Может выйти и «идей нет» — "
                "это честный результат.\n\n"
                "🥊 Сомневаешься в идее? Напиши «/debate <твоё возражение>» — и я НЕ отвечу сам, "
                "а сведу спор разных моделей: одна защищает идею, другая атакует твоим возражением, "
                "третья (слепой судья) выносит вердикт — выдержала идея твоё сомнение или нет.\n\n"
                "Команды: /run-funnel — запустить прогон; /calibrate — калибровочный прогон (копим "
                "статистику точности); /resolve — сверить созревшие прогнозы с фактом; /status — идеи, "
                "ждущие решения; /progress — что я делаю прямо сейчас; /budget — расход на работу "
                "системы; /watchlist — что я отслеживаю; /debate — оспорить идею; /format — короче/"
                "подробнее карточки; /reset — забыть наш диалог.\n\n"
                "Веса и лимиты системы из чата НЕ меняю — это делается командами в терминале (защита "
                "от решений на ходу).")
        elif cmd == "reset":
            self.state["chat_history"] = []
            self.save()
            self.tg.send_message(chat, "Хорошо, начнём разговор с чистого листа.")
        elif cmd == "status":
            pend = [c for c in self.state["pending"].values() if c.get("status") == "pending"]
            if not pend:
                self.tg.send_message(chat, "Сейчас нет идей, ждущих твоего решения.")
            else:
                lines = ["Идеи, ждущие решения:"]
                for c in pend:
                    name = R._asset_human(c["asset"])[0]
                    lock = ("можно брать в работу" if S.accept_unlocked(c["issued_at"])
                            else f"кнопка откроется через {S.hours_remaining(c['issued_at']):.0f} ч")
                    lines.append(f"• {name} ({c['asset']}) — {lock}")
                self.tg.send_message(chat, "\n".join(lines))
        elif cmd in ("progress", "ход", "прогресс"):
            if PROG is None:
                self.tg.send_message(chat, "Индикатор прогресса недоступен (модуль не загрузился).")
            else:
                self.tg.send_message(chat, PROG.format_line())
        elif cmd in ("run-funnel", "run_funnel", "runfunnel", "прогон", "воронка"):
            self._cmd_run_funnel(chat)
        elif cmd == "calibrate":
            self._cmd_calibrate(chat)
        elif cmd == "resolve":
            self._cmd_resolve(chat)
        elif cmd == "debate":
            self._cmd_debate(chat, text)
        elif cmd == "format":
            self._cmd_format(chat, text)
        elif cmd == "budget":
            self.tg.send_message(chat, R.format_budget_line(self._budget_one_liner(with_key=True)))
        elif cmd in ("devops", "предложения"):
            self._cmd_devops(chat)
        elif cmd == "watchlist":
            entries = W.current_entries()
            if not entries:
                self.tg.send_message(chat, "Пока ничего не отслеживаю в режиме ожидания.")
            else:
                lines = ["Отслеживаю (жду нужный момент):"]
                for e in entries.values():
                    t = e.get("trigger")
                    name = R._asset_human(e.get("asset", ""))[0]
                    side = {"above": "поднимется выше", "below": "опустится ниже"}.get(
                        (t or {}).get("dir"), "достигнет уровня") if t else "по сигналу"
                    cond = f"когда цена {side} {t['level']}" if t else "по ручной проверке"
                    lines.append(f"• {name} ({e.get('asset')}) — {cond}")
                self.tg.send_message(chat, "\n".join(lines))
        else:
            self.tg.send_message(chat, "Не знаю такой команды. Напиши /help или просто задай вопрос словами.")

    def _cmd_format(self, chat, text):
        """Переключатель подачи: /format [long|short] | unclear N.. | clear [N..]."""
        parts = text.split()[1:]
        pres = R.load_presentation()
        if not parts:
            unclear = sorted(pres["still_unclear"]) or "—"
            self.tg.send_message(
                chat, f"Формат карточки: {pres['mode']}.\n"
                      f"В коротком режиме ℹ️-подсказки показываются для полей: {unclear}.\n\n"
                      "Команды: /format long — подсказки у всех полей; /format short — только у "
                      "помеченных; /format unclear 3 5 — пометить поля как «ещё мутно»; "
                      "/format clear — убрать все пометки.")
            return
        sub = parts[0].lower()
        if sub in ("long", "short"):
            R.save_presentation(mode=sub)
            self.tg.send_message(chat, f"Готово: формат — {sub}.")
        elif sub == "unclear":
            nums = {int(x) for x in parts[1:] if x.isdigit() and 1 <= int(x) <= 13}
            R.save_presentation(still_unclear=sorted(pres["still_unclear"] | nums))
            self.tg.send_message(chat, f"Помечены как «ещё мутно»: {sorted(pres['still_unclear'] | nums) or '—'}.")
        elif sub == "clear":
            rem = {int(x) for x in parts[1:] if x.isdigit()}
            new = sorted(pres["still_unclear"] - rem) if rem else []
            R.save_presentation(still_unclear=new)
            self.tg.send_message(chat, f"Обновил пометки: {new or '—'}.")
        else:
            self.tg.send_message(chat, "Не понял. /format long|short | unclear N.. | clear [N..]")

    # ── тяжёлые задачи из чата: прогон / калибровка / сверка (фоном, не морозим опрос) ──
    def _job_busy(self):
        return self._job is not None and self._job.is_alive()

    def _start_job(self, label, target):
        """Запускаем target() в фоновом daemon-потоке: полный прогон идёт минуты, а главная
        петля должна продолжать опрашивать Telegram (кнопки решений §12, /progress). Один
        тяжёлый прогон за раз — иначе риск задвоить траты и обойти денежные ворота §11."""
        if self._job_busy():
            return False

        def _wrap():
            try:
                target()
            except Exception as e:                               # noqa: BLE001
                log(label, "ошибка", repr(e))
                try:
                    self.tg.send_message(self.chat_id, f"⚠️ «{label}» не завершилась: {e}")
                except Exception:                                # noqa: BLE001
                    pass
            finally:
                log(label, "фоновая задача завершена")

        self._job_label = label
        self._job = threading.Thread(target=_wrap, name=label, daemon=True)
        self._job.start()
        log("фоновая задача запущена:", label)
        return True

    def _budget_blocked(self, chat, what):
        """True (+сообщение), если достигнут месячный потолок токенов — ворота §11/инвариант 5."""
        try:
            if self._budget_status().get("exit_code") == 3:
                self.tg.send_message(
                    chat, f"На этот месяц достигнут лимит расходов на ИИ-модели — {what} пока не "
                          f"запускаю (защитный лимит §11). Вернётся в начале месяца. "
                          f"Команды /status, /budget, /watchlist работают.")
                return True
        except Exception as e:                                   # noqa: BLE001
            log("budget check ошибка", repr(e))
        return False

    def _busy_notice(self, chat):
        self.tg.send_message(
            chat, f"Уже идёт задача «{self._job_label}». Дождись её конца — ход смотри в /progress. "
                  f"Параллельный запуск не делаю, чтобы не задвоить траты (ворота §11).")

    def _cmd_run_funnel(self, chat):
        """Боевой прогон воронки прямо из Telegram (РЕШЕНИЕ владельца 19.06: event-first,
        открытый скан мира, дешёвый research-режим — skip_contour=True, как ежедневный крон).

        Выдачу пушит штатный _tick_reports (research-дайджест по новому live-протоколу) — здесь
        её не дублируем. mode='live' задаём ЯВНО: иначе протокол пишется как 'auto' и пуш-контур
        принимает его за mock (см. event_first.run_event_first: protocol['mode']=mode без резолва)."""
        if self._job_busy():
            self._busy_notice(chat)
            return
        if self._budget_blocked(chat, "прогон воронки"):
            return
        if not os.environ.get("OPENROUTER_API_KEY"):
            self.tg.send_message(
                chat, "Нет ключа OpenRouter (OPENROUTER_API_KEY) — боевой прогон невозможен. "
                      "Пропиши ключ в .env и перезапусти сервис.")
            return
        self.tg.send_message(
            chat, "🚀 Запускаю боевой прогон: открытый скан мира (новости + цены + тренды) → "
                  "каскады в конкретные компании. Это несколько минут — ход смотри в /progress, "
                  "готовый дайджест пришлю сюда сам. Может выйти и «идей нет» — это честный "
                  "результат (широкий вход, строгое сито), а не поломка.")

        def _job():
            from orchestrator.event_first import run_event_first
            p = run_event_first(mode="live", k=2, skip_contour=True, write=True)
            итог = (p.get("ОТКАЗ_бюджет", {}).get("reason") if p.get("ОТКАЗ_бюджет")
                    else p.get("итог"))           # F0#9: отказ по бюджету тоже логируем внятно
            log("прогон из чата завершён:", p.get("run_id"), "·", итог)

        self._start_job("прогон воронки", _job)

    def _cmd_calibrate(self, chat):
        """Калибровочный прогон §17.3 из чата: пачка мелких прогнозов без ставок — копим
        статистику Brier к воротам Б→Д (270 исходов). Итог пришлём сообщением."""
        if self._job_busy():
            self._busy_notice(chat)
            return
        if self._budget_blocked(chat, "калибровку"):
            return
        self.tg.send_message(
            chat, "📐 Запускаю калибровочный прогон — мелкие прогнозы без ставок, чтобы копить "
                  "статистику точности (Brier). Итог пришлю сюда.")

        def _job():
            from orchestrator import calibrate as CAL
            s = CAL.run_calibrate(mode="auto", write=True)
            if "ОТКАЗ" in s:
                self.tg.send_message(self.chat_id, f"Калибровка не выполнена: {s['ОТКАЗ']}.")
                return
            self.tg.send_message(
                self.chat_id,
                f"📐 Калибровка готова: сгенерировано {s['сгенерировано']}, разрешимо §9 "
                f"{s['разрешимо_§9']}, запечатано {s['запечатано']}. Всего исходов разрешено "
                f"{s['разрешено_исходов']}, Brier {s['текущий_brier']}, до ворот 270 осталось "
                f"{s['до_ворот_270']}.")

        self._start_job("калибровка", _job)

    def _cmd_resolve(self, chat):
        """Сверка созревших прогнозов с фактом §10.10 из чата. Детерминированно, без трат
        токенов (бюджет не трогаем), поэтому без ворот §11."""
        if self._job_busy():
            self._busy_notice(chat)
            return
        self.tg.send_message(chat, "🔍 Сверяю созревшие прогнозы с фактическими исходами…")

        def _job():
            from orchestrator import resolve as RES
            s = RES.run_resolve(write=True)
            msg = (f"🔍 Сверка готова: сверено сейчас {s['сверено_сейчас']}, ещё ждут "
                   f"{s['ещё_pending']}. Всего исходов {s['всего_исходов']}, Brier {s['brier']}, "
                   f"калибровка band {s['калибровка_band_пп']} п.п., до ворот 270 осталось "
                   f"{s['до_ворот_270']}.")
            if s.get("ошибок"):
                msg += f"\n⚠ ошибок сверки: {s['ошибок']}."
            self.tg.send_message(self.chat_id, msg)

        self._start_job("сверка исходов", _job)

    # ── состязательный разбор идеи по возражению (§4 блок E) ─────────────────────────
    def _cmd_debate(self, chat, text):
        """/debate <возражение> — НЕ ответ Дирижёра, а спор разных семейств моделей по идее.

        Генератор/адвокат защищают тезис, критик атакует возражением владельца, слепой судья
        (другого семейства, П10) выносит вердикт. Можно начать с тикера выданной идеи: «/debate
        SPY.US это просто ставка на рынок». Без тикера — последняя выданная идея."""
        # Денежные ворота §11: контур = 5 LLM-вызовов; при превышении потолка не жжём токены.
        try:
            if self._budget_status().get("exit_code") == 3:
                self.tg.send_message(
                    chat, "На этот месяц достигнут лимит расходов на ИИ-модели — состязательный "
                          "разбор пока не запускаю (защитный лимит). Вернётся в начале месяца.")
                return
        except Exception as e:                                   # noqa: BLE001
            log("debate budget check ошибка", repr(e))

        sys.path.insert(0, str(ROOT))
        from orchestrator import challenge as CH

        parts = text.split(maxsplit=1)
        doubt = parts[1].strip() if len(parts) > 1 else ""
        ideas = CH.list_ideas()
        if not doubt:
            if not ideas:
                self.tg.send_message(chat, "Пока нет выданных идей, которые можно оспорить — "
                                           "дождись прогона воронки.")
                return
            lines = ["Какую идею оспорить? Напиши «/debate <твоё сомнение>» — разберу последнюю; "
                     "или начни с тикера. Сейчас на столе:"]
            for i in ideas:
                lines.append(f"• {R._asset_human(i['актив'])[0]} ({i['актив']}) {i['направление']}")
            self.tg.send_message(chat, "\n".join(lines))
            return

        # Необязательный ведущий тикер: «/debate SPY.US ...» — только если совпал с выданной идеей.
        asset = None
        assets = {i["актив"] for i in ideas}
        tok = doubt.split()[0] if doubt.split() else ""
        for cand in (tok.upper(), tok.upper() + ".US"):
            if cand in assets:
                asset = cand
                doubt = doubt[len(tok):].strip() or doubt
                break

        # Разбор = 5 последовательных вызовов LLM (минуты). Уводим в ФОНОВЫЙ поток, как все
        # тяжёлые команды (_cmd_run_funnel/_cmd_calibrate/...), иначе главный цикл опроса
        # блокируется на всё время разбора — бот перестаёт отвечать и реагировать на кнопки
        # (баг «запустил разбор → бот завис»). Один тяжёлый прогон за раз (ворота §11).
        if self._job_busy():
            self._busy_notice(chat)
            return
        self.tg.send_message(
            chat, "🥊 Запускаю состязательный разбор: одна модель защищает идею, другая атакует "
                  "твоим возражением, слепой судья решает. Это займёт до минуты…")
        if hasattr(self.tg, "send_chat_action"):
            self.tg.send_chat_action(chat, "typing")

        def _job():
            # Доставка результата — ВНУТРИ задачи: при сбое отправки _start_job._wrap его
            # поймает, сообщит владельцу и залогирует (раньше _send_long стоял вне try и сбой
            # терялся молча).
            p = CH.run_challenge(doubt, asset=asset, mode="auto", write=True)
            if "ОТКАЗ" in p:
                self.tg.send_message(self.chat_id, f"Не получилось: {p['ОТКАЗ']}.")
                return
            self._send_long(
                self.chat_id, p["резюме"] + "\n\n(Это исследовательский разбор, не рекомендация — "
                                            "решение и риск за тобой, §12.)")
            log("debate: разбор отправлен", p.get("run_id"))

        self._start_job("разбор идеи (debate)", _job)

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
                    chat, "На этот месяц достигнут лимит расходов на ИИ-модели — отвечать словами "
                          "пока не могу (это защитный лимит). Команды /status, /budget, /watchlist "
                          "работают; диалог вернётся в начале месяца.")
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

    def _send_card(self, text, kb):
        """Длинную карточку шлём частями (лимит Telegram 4096); клавиатуру — на ПОСЛЕДНЮЮ часть.
        Ревью 2026-07-04: None, если ХОТЬ ОДНА часть не дошла (Telegram._call при ошибке возвращает
        None) — вызывающий обязан НЕ помечать доставленным и ретраить в следующий tick."""
        import bot_chat as CH
        parts = [text[i:i + CH.MAX_REPLY_CHUNK] for i in range(0, len(text), CH.MAX_REPLY_CHUNK)] or [text]
        mid, ok = None, True
        for i, part in enumerate(parts):
            last = (i == len(parts) - 1)
            m = self.tg.send_message(self.chat_id, part, reply_markup=kb if last else None)
            ok = ok and (m is not None)
            if last:
                mid = m
        return mid if ok else None

    def _tick_reports(self):
        pres = R.load_presentation()
        for proto in R.new_runs(self.state["pushed_runs"]):
            run_id = proto.get("run_id")
            issued_at = proto.get("ts")
            # МОКА — только явный mode=="mock" (дымовой прогон без сети/трат). Режим "auto"
            # боевой: run.py трактует auto→live при наличии ключа OpenRouter (event_first/funnel
            # по крону идут как auto и делают реальную работу). Раньше is_mock=(mode!="live")
            # ошибочно глотал ВСЕ кроновые auto-прогоны в тишину (journal/bot.log: «mock-прогон
            # не отправлен») — до пользователя долетал лишь бюджет. Фикс: auto и live доставляем.
            is_mock = (proto.get("mode") == "mock")
            # mock/test НЕ маскируем под идею: по умолчанию вовсе не шлём (config флаг).
            if is_mock and not pres["send_mock_to_telegram"]:
                self.state["pushed_runs"].append(run_id)
                log("mock-прогон не отправлен (send_mock_to_telegram=false)", run_id)
                continue
            if is_mock:
                self.tg.send_message(self.chat_id, R.format_mock_check(proto))
                self.state["pushed_runs"].append(run_id)
                log("пуш проверки связи (mock)", run_id)
                continue
            # Event-first СВОДНЫЙ прогон (ef_<ts>, без '__') → research-дайджест (РЕШЕНИЕ A, анти-brent):
            # поток идей по событиям мира на компаниях, без кнопок-ставок (research-метка §16).
            if str(run_id).startswith("ef_") and "__" not in str(run_id):
                # Ревью 2026-07-04 HIGH: run_id раньше помечался pushed НЕЗАВИСИМО от успеха отправки —
                # сбой сети/лимита Telegram в момент пуша терял money-карточку §11 НАВСЕГДА.
                # Теперь: дайджест имеет свой маркер доставки (не дублируется при ретрае карточек),
                # run помечается pushed ТОЛЬКО когда всё дошло; недоставленное ретраится следующим tick.
                self.state.setdefault("digest_sent", [])
                if run_id not in self.state["digest_sent"]:
                    # дайджест с разбором новость→цепочка→суд длиннее лимита Telegram (4096) → частями.
                    if self._send_card(R.format_research_digest(proto), None) is None:
                        log("сбой отправки дайджеста — ретрай в следующий tick", run_id)
                        continue
                    self.state["digest_sent"].append(run_id)
                # §11 fail-closed: money-каскады, ПЕРЕЖИВШИЕ слепой суд («УСТОЯЛА», без вето §6),
                # доходят до пользователя ОТДЕЛЬНОЙ §8-actionable карточкой с кнопками §12. Дайджест —
                # обзорный поток, карточка — решение по конкретной идее: РАЗНЫЕ форматы одной идеи,
                # не дубль (а одну и ту же карточку дважды не шлём — гард по токену в pending). Метку
                # «исследование, НЕ инвестрекомендация» НЕ снимаем (C2/FГ: калибр-гейт §11 не пройден).
                money_ideas = R.money_ideas_from_protocol(proto)
                proto_card = dict(proto)
                if proto_card.get("mode") == "auto":     # auto боевой (run.py: auto→live при ключе) —
                    proto_card["mode"] = "live"           # рендерим полную §8-карточку, не mock-заглушку
                доставлено_всё = True
                for idea in money_ideas:
                    asset = idea.get("актив")
                    token = S.idea_token(run_id, asset)
                    if token in self.state["pending"]:   # идемпотентность: не слать карточку дважды
                        continue
                    text = R.format_report(proto_card, idea, pres)
                    kb = R.build_keyboard(token, issued_at)
                    mid = self._send_card(text, kb)
                    if mid is None:                      # карточка НЕ дошла → pending не пишем, ретрай
                        доставлено_всё = False
                        log("сбой отправки money-карточки — ретрай в следующий tick", run_id, asset)
                        continue
                    self.state["pending"][token] = {
                        "run_id": run_id, "asset": asset,
                        "direction": idea.get("направление"), "score": idea.get("балл"),
                        "issued_at": issued_at, "message_id": mid, "status": "pending",
                        "idea_brief": R.idea_brief(idea),
                    }
                    log("пуш money-карточки §8 (пережила слепой суд)", run_id, asset, "token", token)
                if not доставлено_всё:
                    continue                             # run НЕ помечен pushed — недоставленное ретраится
                added = W.ingest_protocol(proto, existing_ids=self.state["seen_watchlist"])
                for rec in added:
                    self.state["seen_watchlist"].append(rec["id"])
                self.state["pushed_runs"].append(run_id)
                log("пуш research-дайджеста event-first", run_id)
                continue
            ideas = R.ideas_from_protocol(proto)
            if ideas:
                доставлено_всё = True
                for idea in ideas:
                    asset = idea.get("актив")
                    token = S.idea_token(run_id, asset)
                    if token in self.state["pending"]:   # идемпотентность при ретрае (ревью 04.07)
                        continue
                    text = R.format_report(proto, idea, pres)
                    kb = R.build_keyboard(token, issued_at)
                    mid = self._send_card(text, kb)
                    if mid is None:                      # не дошла → pending не пишем, ретрай tick'ом
                        доставлено_всё = False
                        log("сбой отправки идеи — ретрай в следующий tick", run_id, asset)
                        continue
                    self.state["pending"][token] = {
                        "run_id": run_id, "asset": asset,
                        "direction": idea.get("направление"), "score": idea.get("балл"),
                        "issued_at": issued_at, "message_id": mid, "status": "pending",
                        # суть идеи — чтобы свободный чат Дирижёра мог предметно её обсуждать
                        # даже после ротации файлов протоколов (§8 поля 1/2/11 + P судьи).
                        "idea_brief": R.idea_brief(idea),
                    }
                    log("пуш идеи", run_id, asset, "token", token)
                if not доставлено_всё:
                    continue                             # run НЕ помечен pushed — ретрай недоставленного
            else:
                if self.tg.send_message(self.chat_id, R.format_weak_day(proto)) is None:
                    log("сбой отправки «идей нет» — ретрай в следующий tick", run_id)
                    continue
                log("пуш «идей нет»", run_id)
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
            self.tg.send_message(self.chat_id, "🤖 «Оракул» на связи. Буду присылать идеи простым "
                                               "языком и отвечать на вопросы. Нажми /help, чтобы понять, "
                                               "что я умею.")
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
