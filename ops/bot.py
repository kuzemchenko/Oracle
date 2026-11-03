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
# Перепроверка №3 (03.11): охотник стартует 10:17 UTC и ищет 1–2 ч, сервер забирает в :05 — штатная доставка
# 12–14 UTC; сторож «идея дня пока не готова» в 13:00 будил владельца раньше неё. Теперь 15:00 UTC.
WATCHDOG_HOUR_UTC = 15


def log(*a):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(ts, LOG_TAG, *a, flush=True)


# ── тонкий Telegram-клиент ──────────────────────────────────────────────────────────
DELIVERIES_PATH = None      # журнал доставок; None = ROOT/journal/deliveries.jsonl (тесты подменяют)

# Аудит 03.11 (датчики V1): алерт называет причину по коду выхода и готовое действие владельцу
OWNER_PULL_CMD = ("sudo systemctl stop oracle-bot && cd /home/oracle/oracle && sudo -u oracle git pull "
                  "--rebase --autostash origin master && crontab -u oracle ops/crontab.txt && "
                  "sudo systemctl start oracle-bot")
PULL_CODES = {   # ops/safe_pull.sh
    "2": ("нет связи с GitHub", "ничего — повторю через час; если так весь день, проверь сеть сервера"),
    "3": ("ветки сервера и GitHub разошлись", "одна ручная протяжка: " + OWNER_PULL_CMD),
    "8": ("git был занят другой операцией дольше 15 минут", "ничего — повторю через час"),
    "9": ("облачный коммит тронул журналы честности — сервер его не пускает (так и задумано)",
          "не протягивать вручную; напиши мне в сессию — разберу коммит"),
}
# Аудит 03.11 (P1 №24): задачи, которые бот запускает сам за флагами config/presentation.yaml — «ок»
# владельца становится облачным коммитом флага (сервер протянет его за час), без захода в терминал ради
# crontab. ВНИМАНИЕ: при включённом флаге одноимённую строку в crontab НЕ раскомментировать (двойной прогон).
SCHEDULED_JOBS = (
    # флаг,              метка в алертах, команда (от корня проекта),                    дни недели,      не раньше (UTC)
    ("deep_dive_auto", "deep_dive", ("orchestrator/deep_dive.py", "--mode", "live"), (1, 2, 3, 4, 5, 6, 7), (9, 40)),
    ("week_odds_auto", "week_odds", ("orchestrator/week_odds.py", "--mode", "live", "--seal"), (2, 4), (8, 10)),
    ("point_b_auto", "point_b", ("ops/point_b.py", "--write"), (5,), (17, 30)),
    # снимок серверных закрытий для облачного охотника — до чекпоинта 22:00 (без LLM, только чтение базы)
    ("price_snapshot_auto", "price_snapshot", ("ops/price_snapshot.py", "write"), (1, 2, 3, 4, 5, 6, 7), (21, 30)),
)
CHECKPOINT_CODES = {   # ops/checkpoint.sh
    "3": "репозиторий в середине слияния — нужна ручная протяжка: " + OWNER_PULL_CMD,
    "4": "в индексе git чужие изменения (ручная работа на сервере) — закончи её или пришли мне git status",
    "5": "не удалось добавить файлы журналов в git — пришли мне хвост journal/cron.log",
    "6": "не удалось сделать коммит журналов — пришли мне хвост journal/cron.log",
    # ре-гейт 03.11 (П16): прежний сервер отдаёт 7 и при «облако тронуло журналы» — совет протянуть вручную
    # обходил бы охрану журнала. Поэтому сначала проверка по алерту протяжки того же дня
    "7": ("главная ветка в GitHub заморожена или ветки разошлись. Сначала посмотри: если сегодня был алерт "
          "протяжки с кодом 9 (облако тронуло журналы) — вручную НЕ протягивай, напиши мне. Иначе — одна "
          "ручная протяжка: " + OWNER_PULL_CMD),
    "9": ("облачный коммит тронул журналы честности — журналы сервера сохранены в запасную ветку "
          "server-journal, ничего не потеряно; вручную НЕ протягивай, напиши мне — разберу коммит"),
    "8": "git был занят другой операцией дольше 15 минут — повторю завтра",
}


class Telegram:
    def __init__(self, token, session=None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
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

    def get_file(self, file_id):
        """Метаданные файла (file_path) для скачивания — голосовые владельца (STT)."""
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_path):
        """Байты файла с CDN Telegram (лимит Bot API 20 МБ — голосовым хватает с запасом)."""
        try:
            r = self.s.get(f"{self.file_base}/{file_path}", timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            log("telegram download ошибка", repr(e))
            return None
        if r.status_code != 200:
            log("telegram download !200", r.status_code)
            return None
        return r.content


# ── сервис ──────────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self, tg, chat_id, state=None):
        self.tg = tg
        self.chat_id = str(chat_id) if chat_id else None
        self.state = state if state is not None else S.load_state()
        self._job = None            # текущая фоновая задача (прогон/калибровка/сверка)
        self._job_label = None      # её человеческое имя для /progress и сообщений

    # Кэп бессрочно растущих списков state (ревью 04.07 LOW): помним хвост — старые run_id/уже
    # увиденные записи никогда не понадобятся (протоколы старше и так не «новые» для new_runs).
    _STATE_CAPS = {"pushed_runs": 500, "digest_sent": 500, "seen_watchlist": 1000}

    def save(self):
        for key, cap in self._STATE_CAPS.items():
            v = self.state.get(key)
            if isinstance(v, list) and len(v) > cap:
                self.state[key] = v[-cap:]
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
        # V2 (карта v3): кнопки «Идеи дня» (v:...) и еженедельной карточки тем (t:...).
        if len(parts) == 3 and parts[0] == "v":
            self._on_idea_day_cb(cb_id, chat, msg, parts[1], parts[2])
            return
        if len(parts) == 3 and parts[0] == "t":
            self._on_theme_cb(cb_id, chat, msg, parts[1], parts[2])
            return
        if len(parts) == 3 and parts[0] == "p":
            self._on_proposal_cb(cb_id, chat, msg, parts[1], parts[2])
            return
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
        # Голосовое → текст (STT, ops/bot_voice) → дальше ОБЫЧНЫЙ путь: голос эквивалентен
        # печати (мотив, «дай идеи», «разбери ТИКЕР», свободный вопрос). Раньше голосовые
        # молча отбрасывались здесь пустым text (владелец наговаривал — бот не реагировал).
        if not text and (msg.get("voice") or msg.get("audio")):
            text = self._voice_text(chat, msg) or ""
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
        # Этап3: ответ на вопрос «Разбора дня» («Твой ход: …») → разметка качества в decisions_user.
        # Ловим ТОЛЬКО короткий текст, совпавший с показанным вариантом (иначе это обычный вопрос).
        if not text.startswith("/") and self._capture_case_feedback(chat, text):
            return
        # Flow-fix В1: ответ «1»/«2»/«3» на «Топ-3» → мгновенная защита выбранной идеи из готового суда.
        if not text.startswith("/") and self._capture_top3_pick(chat, text):
            return
        # V0: «что ещё было» после «Идеи дня» → остальные идеи того же прогона списком (Топ-3).
        if not text.startswith("/") and self._intent_more_ideas(text):
            self._cmd_more_ideas(chat)
            return
        if text.startswith("/"):
            self._command(chat, text)
            return
        # R9c: «проверь идею <X>» → полный состязательный контур по идее пользователя.
        idea = self._intent_check_idea(text)
        if idea:
            self._cmd_check_idea(chat, idea)
            return
        # П3 (§17.6): «сессия», «дай идеи» → сессия партнёра из ГОТОВОГО прогона (без запуска).
        if self._intent_session(text):
            self._cmd_session(chat)
            return
        # Намерение в свободной речи: «сделай прогон», «найди идеи» → боевой прогон (узкий
        # вайтлист, чтобы не перехватывать настоящие вопросы). Прочее — диалог с Дирижёром.
        if self._intent_run_funnel(text):
            self._cmd_run_funnel(chat)
            return
        # Свободный текст → диалог с Дирижёром (как в терминале).
        self._chat(chat, text)

    def _capture_case_feedback(self, chat, text):
        """Этап3: если последним был показан «Разбор дня» и владелец ответил коротким текстом,
        совпавшим с одним из предложенных вариантов, — пишем разметку в decisions_user.jsonl (§25)
        и подтверждаем. Иначе False (пусть текст обработают обычные интенты/Дирижёр). Строго:
        ловим только явное совпадение варианта, чтобы не перехватывать реальные вопросы."""
        case = self.state.get("last_case")
        if not case:
            return False
        варианты = case.get("варианты") or []
        t = text.lower().strip().strip(".!?…")
        if len(t) > 40:                                  # длинный текст — это вопрос, не выбор варианта
            return False
        # ТОЧНОЕ совпадение с вариантом (целиком или его смысловой частью до запятой, если та ≥4 букв
        # и не служебное да/нет/не). Подстрочный матч по «голове» глотал обычные короткие сообщения
        # владельца («да»/«нет»/«не знаю») и писал ложную разметку — поэтому только полное совпадение.
        matched = None
        _stop = {"да", "нет", "не", "ок", "нужен", "нужно"}
        for v in варианты:
            vl = v.lower().strip()
            head = vl.split(",")[0].strip()
            if t == vl or (t == head and len(head) >= 4 and head not in _stop):
                matched = v
                break
        if not matched:
            return False
        S.append_case_feedback(run_id=case.get("run_id"), asset=case.get("asset"),
                               status=case.get("status"), answer=matched, chat_id=self.chat_id)
        self.state["last_case"] = None
        self.save()
        self.tg.send_message(chat, "Записал твою оценку кейса — она идёт в счёт качества выдачи (§25). "
                                   "Хочешь глубже — «разбери ТИКЕР».")
        return True

    def _capture_top3_pick(self, chat, text):
        """Flow-fix В1: если последним показан «Топ-3» и владелец ответил номером (1–3, «разбери 2»,
        «первая»…) — раскрываем защиту этой идеи из ГОТОВОГО суда (мгновенно, без прогона). Строго:
        ловим только явный выбор номера, чтобы не перехватывать обычные сообщения. Иначе False."""
        lt = self.state.get("last_top3")
        if not lt or not (lt.get("idx")):
            return False
        t = text.lower().strip().strip(".!?…")
        if len(t) > 24:                                  # длинный текст — это вопрос, не выбор номера
            return False
        for pre in ("разбери ", "раскрой ", "покажи ", "идея ", "идею ", "номер ", "№", "давай "):
            if t.startswith(pre):
                t = t[len(pre):].strip()
        t = {"первая": "1", "первую": "1", "первый": "1", "вторая": "2", "вторую": "2", "второй": "2",
             "третья": "3", "третью": "3", "третий": "3"}.get(t, t)
        if t not in ("1", "2", "3"):
            return False
        ticker = (lt.get("idx") or {}).get(t)
        if not ticker:
            self.tg.send_message(chat, f"Под номером {t} сегодня идеи нет.")
            return True
        proto = R.load_protocol(lt.get("run_id"))
        if not proto:
            self.tg.send_message(chat, "Не нашёл прогон, из которого была та подборка — сделай свежий "
                                       "(«найди идеи») и выбери заново.")
            return True
        card, viol = R.top3_defense_card(proto, ticker)
        if card is None:
            log("Топ-3 защита не прошла стиль-контракт", lt.get("run_id"), ticker, viol)
            self.tg.send_message(chat, f"Не смог собрать чистую защиту по {ticker}. Попробуй "
                                       f"«разбери {ticker}» — полный разбор.")
            return True
        self._send_card(card, None)
        return True

    # ── V2 (карта v3): кнопки «Идеи дня» и карточки тем ─────────────────────────────
    def _on_idea_day_cb(self, cb_id, chat, msg, code, run_id):
        """Кнопки под «Идеей дня»: q=Качаем 2 дня, w=Что ещё было, m=Мимо.

        Качаем/Мимо пишутся в journal/decisions_user.jsonl (§12, append-only) — впервые
        у ежедневного пуша есть действие в один клик (аудит 31.10: ноль нажатий за жизнь,
        потому что нажимать было не на что). «Качаем» дополнительно ставит идею в очередь
        прокачки journal/deep_dive.jsonl (двухдневный конвейер V3) и СРАЗУ запускает
        полный состязательный разбор тикера (существующий контур «разбери ТИКЕР»)."""
        lid = self.state.get("last_idea_day") or {}
        if lid.get("run_id") != run_id:
            # перепроверка №2: у каждой идеи охотника свои кнопки — не только у последней
            lid = (self.state.get("hunter_lids") or {}).get(run_id) or {}
        if not lid or lid.get("run_id") != run_id:
            self.tg.answer_callback(cb_id, "Эта подборка устарела — свежая придёт утром.")
            return
        lead = lid.get("lead")
        issued_at = lid.get("issued_at")
        if code == "w":
            self.tg.answer_callback(cb_id)
            self._cmd_more_ideas(chat)
            return
        _motives = {"m1": "не понял", "m2": "не верю", "m3": "поздно"}
        if code not in ("q", "m", "p", *_motives) or not lead:
            self.tg.answer_callback(cb_id)
            return
        already = lid.get("action")
        if already:
            self.tg.answer_callback(cb_id, f"Уже {S.action_ru(already)}.")
            return
        action = {"q": "deep_dive", "p": "paper"}.get(code, "skip")
        S.append_decision(run_id=run_id, asset=lead, direction=None, score=None,
                          action=action, issued_at=issued_at, chat_id=self.chat_id)
        if code in _motives:                         # причина отказа — отдельной строкой (§12)
            S.append_motive(run_id=run_id, asset=lead, action="skip",
                            motive=_motives[code], chat_id=self.chat_id)
        lid["action"] = action
        if (self.state.get("last_idea_day") or {}).get("run_id") == run_id:
            self.state["last_idea_day"] = lid
        if lid.get("source") == "hunter":
            self.state.setdefault("hunter_lids", {})[run_id] = lid
        if msg.get("message_id"):      # кнопки сняли — решение одно на идею дня
            self.tg.edit_reply_markup(chat, msg["message_id"], {"inline_keyboard": []})
        if action == "skip":
            self.tg.answer_callback(cb_id, "Понял: мимо.")
            why = _motives.get(code)
            # аудит 03.11: прежнее «учитываю» было обещанием без исполнителя. Теперь причину
            # читает облачный охотник (ops/owner_feedback.py; журнал доезжает вечерним чекпоинтом).
            use = {"поздно": "строже проверит, не ушла ли уже цена",
                   "не верю": "отдаст твоё сомнение второй проверке и ответит на него в записке",
                   "не понял": "напишет следующую записку проще"}.get(why)
            self.tg.send_message(chat, "Записал «мимо»" + (f" — {why}" if why else "") + ". " +
                                 (f"Охотник прочитает причину при следующем поиске и {use}. " if use else
                                  "Если скажешь почему (кнопки «не понял / не верю / поздно»), охотник "
                                  "учтёт причину при следующем поиске. ") +
                                 "«Что ещё было» покажет остальные идеи дня.")
            self.save()
            return
        if action == "paper":
            self.tg.answer_callback(cb_id, "Записал на бумагу.")
            promise = ("В дату сверки пришлю итог прогноза — сбылось или нет."
                       if lid.get("source") == "hunter" and lid.get("sealed")
                       else "Итог этой идеи увидишь в недельном разборе (/review-week).")
            self.tg.send_message(chat, "Записал «беру на бумагу»: это учебная сделка без денег. " + promise)
            self.save()
            return
        # Качаем: очередь прокачки V3 + немедленный состязательный разбор (готовый контур).
        self.tg.answer_callback(cb_id, "Качаем!")
        if lid.get("source") == "hunter":
            # перепроверка №4: идею охотника НЕ гоним в старую воронку — её слепой суд разбивает почти
            # всё («дал идею — сам убил» прямо под кнопкой). Показываем, на чём идея стоит: карточку
            # (факты с источниками, похожий случай, что опровергнет) и очищенное сомнение суда.
            self._append_deep_dive(run_id, lead)
            self._send_card(self._hunter_dossier(lid), None)
            self.save()
            return
        self._append_deep_dive(run_id, lead)
        name = R._asset_human(lead)[0]
        self.tg.send_message(chat, f"Взял {name} ({lead}) в прокачку. Сейчас прогоню полный "
                                   f"состязательный разбор, а конвейер двухдневной прокачки "
                                   f"добавит углубления по утрам.")
        self.save()
        self._cmd_check_idea(chat, lead)

    def _hunter_dossier(self, lid):
        """«Качаем» по идее охотника: досье из её карточки (то, что прошло шлагбаум и суд) — тезис,
        факты с датой и источником, похожий случай и контрпример, что опровергнет. Фрагменты с
        вероятностями/шансами отбрасываются (П8/Инв#6); вероятность прогноза не показывается."""
        try:
            from ops import presentation_lint as PL, hunter_seal as HS
        except ImportError:
            import presentation_lint as PL
            import hunter_seal as HS
        import json as _json
        from urllib.parse import urlparse
        name = lid.get("name") or str(lid.get("lead") or "").split(".")[0]
        base = str(lid.get("lead") or "").split(".")[0]
        card = {}
        f = lid.get("idea_file")
        if f:
            try:
                card = _json.loads(HS.card_path_for(self._ideas_dir() / f).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                card = {}

        def ok(t):
            t = " ".join(str(t or "").split())
            return t if t and not PL.odds_markers(t, include_quotes=True) else ""

        out = [f"Взял {name} ({base}) в прокачку — вот на чём стоит идея: материалы её карточки "
               "(факты с датой публикации или проверки и источником)."]
        th = ok(card.get("thesis"))
        if th:
            out.append("Суть. " + th[:700])
        facts = [x for x in (card.get("facts") or []) if isinstance(x, dict)]
        rows = []
        for x in facts[:8]:
            t = ok(x.get("text"))
            if t:
                dom = urlparse(str(x.get("url") or "")).netloc.removeprefix("www.")
                # перепроверка №6: только точный маркер линзы фактов «ставку не несёт» (а не любое
                # «не подтверждено» — так помечался подтверждённый опорный факт)
                mark = "(не подтверждено, ставку не несёт) " if "ставку не несёт" in t.lower() else ""
                when = (f"проверено {x.get('date')}" if str(x.get("date_kind") or "").lower() == "проверка"
                        else str(x.get("date")))
                rows.append(f"• {mark}{t[:260]} ({when}, {dom})")
        if rows:
            out.append("Факты с источниками:\n" + "\n".join(rows))
        pr = card.get("precedent") if isinstance(card.get("precedent"), dict) else {}
        for label, key in (("Похожий случай", "text"), ("Контрпример", "counter")):
            t = ok(pr.get(key))
            if t:
                out.append(f"{label}. {t[:600]}")
        kills = [k for k in (card.get("kill_conditions") or []) if isinstance(k, dict)]
        krows = [f"• до {k.get('check_by')}: {ok(k.get('what'))[:240]}" for k in kills[:6] if ok(k.get("what"))]
        if krows:
            out.append("Что меня опровергнет и когда проверю:\n" + "\n".join(krows))
        if lid.get("court_objection"):
            out.append("Сомнение второй проверки (модели других разработчиков, вслепую): «"
                       + str(lid["court_objection"]) + "».")
        if f:
            out.append("Полное исследование со всеми ссылками — reports/ideas/" + f.replace(".push.txt", ".md")
                       + " в репозитории.")
        if not card:
            out.append("Карточка идеи на сервере не найдена — досье неполное; напиши «разбери "
                       + base + "», если нужен независимый разбор.")
        out.append("Это " + PL.RESEARCH_FRAME + ".")
        return "\n\n".join(out)

    def _append_deep_dive(self, run_id, asset):
        """Очередь двухдневной прокачки (V3 читает по крону). Append-only, не журнал §16."""
        import json as _json
        import datetime as _dt
        path = ROOT / "journal" / "deep_dive.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                   "run_id": run_id, "asset": asset, "status": "queued",
                   "source": "button_deep_dive"}
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log("deep_dive queue ошибка", repr(e))

    def _on_theme_cb(self, cb_id, chat, msg, code, key):
        """Кнопки еженедельной карточки тем: y=взять под наблюдение, n=мимо.
        Решение → journal/theme_decisions.jsonl; конфиг вносит машинист через git (П16/§30)."""
        import bot_themes as TH
        pend = self.state.get("themes_pending") or {}
        row = pend.get(key)
        if row is None:
            self.tg.answer_callback(cb_id, "Эта тема уже разобрана или карточка устарела.")
            return
        decision = "approved" if code == "y" else "rejected"
        TH.record_decision(key, decision, row, self.chat_id)
        pend.pop(key, None)
        self.state["themes_pending"] = pend
        self.tg.answer_callback(cb_id, "Взял ✅" if decision == "approved" else "Мимо ❌")
        if msg.get("message_id"):                  # аудит 03.11: разобранная тема — без кнопок
            rows = [r for r in ((msg.get("reply_markup") or {}).get("inline_keyboard") or [])
                    if not any(str(b.get("callback_data", "")).endswith(":" + key) for b in r)]
            self.tg.edit_reply_markup(chat, msg["message_id"], {"inline_keyboard": rows})
        if not pend:
            self.tg.send_message(chat, "Пакет тем разобран, спасибо. С ближайшей охоты одобренные темы "
                                       "идут первыми в зоны поиска идеи дня.")
        self.save()

    def _on_proposal_cb(self, cb_id, chat, msg, code, key):
        """Кнопки карточки предложений §25: y=подписываю (авторизация §10, вносит машинист
        через git), n=отклонить. Решение → journal/proposal_decisions.jsonl (append-only)."""
        import bot_proposals as PR
        pend = self.state.get("proposals_pending") or {}
        row = pend.get(key)
        if row is None:
            self.tg.answer_callback(cb_id, "Это предложение уже разобрано или карточка устарела.")
            return
        if code not in ("y", "n"):                 # неизвестная кнопка — не пишем в append-only журнал
            self.tg.answer_callback(cb_id)
            return
        decision = "signed" if code == "y" else "rejected"
        PR.record_decision(key, decision, row, self.chat_id)
        pend.pop(key, None)
        self.state["proposals_pending"] = pend
        self.tg.answer_callback(cb_id, "Подписал ✅ — внесёт машинист с проверкой."
                                if decision == "signed" else "Отклонено ❌")
        if not pend:
            self.tg.send_message(chat, "Пакет предложений разобран, спасибо. Подписанное "
                                       "машинист внесёт через git с ревью и тестами.")
        self.save()

    @staticmethod
    def _idea_kb(rid):
        """Кнопки под идеей дня (разбор 02.11, метрика полезности «отклик»): действие и ПРИЧИНА
        отказа — «не понял» лечится подачей, «не верю» — судом, «поздно» — отбором по цене."""
        return {"inline_keyboard": [
            [{"text": "🔍 Качаем 2 дня", "callback_data": f"v:q:{rid}"},
             {"text": "📝 Беру на бумагу", "callback_data": f"v:p:{rid}"},
             {"text": "📋 Что ещё было", "callback_data": f"v:w:{rid}"}],
            [{"text": "Мимо: не понял", "callback_data": f"v:m1:{rid}"},
             {"text": "Мимо: не верю", "callback_data": f"v:m2:{rid}"},
             {"text": "Мимо: поздно", "callback_data": f"v:m3:{rid}"}],
        ]}

    def _tick_hunter_ideas(self):
        """Доставка идей ОХОТНИКА (облачная охота → reports/ideas/*.push.txt → git pull →
        этот тик → Telegram). Вопрос владельца 02.11: «идея MP так и не попала в Telegram».

        Гейт = lint_hunter_idea (правила «Идеи дня» + рамка «не рекомендация» КОДОМ + запрет
        вероятностных слов в любой форме). Флаг hunter_push в presentation.yaml.

        Архитектура охоты 02.11 (вердикт «дал идею — сам убил»): идея охотника — ГЛАВНАЯ идея
        дня. Перед доставкой бот (сервер, П16) печатает её §9-карточку *.card.json в журнал
        отдельным треком hunter_idea (ops/hunter_seal.py) и дописывает КОДОМ строку о печати и
        «счёт метода»; под пушем — кнопки [Качаем 2 дня][Что ещё было][Мимо] (петля §12).

        Отметки state по-событийно (сбой сети не дублирует пуши). Ключ = имя+хеш содержимого:
        доставленная версия не повторяется; изменённый после доставки файл уходит ОДИН раз с
        пометкой «обновлённая версия»; брак/нет-источников помечаются суффиксом и не вечны."""
        if not R.load_presentation().get("hunter_push"):
            return
        try:
            from ops import presentation_lint as PL
        except ImportError:
            import presentation_lint as PL
        try:
            from ops import hunter_seal as HS
            from ops import hunter_court as HC
        except ImportError:
            import hunter_seal as HS
            import hunter_court as HC
        d = self._ideas_dir()
        if not d.exists():
            return
        seen = set(self.state.get("hunter_pushed") or [])          # все отметки (брак/нет-md/…)
        delivered = set(self.state.get("hunter_delivered") or [])  # ТОЛЬКО доставленные версии
        footers = self.state.setdefault("hunter_footer", {})
        retry = self.state.setdefault("hunter_retry", {})           # версия → когда повторить
        now = S.now_utc()

        def _mark(name):
            seen.add(name)
            self.state["hunter_pushed"] = sorted(seen)
            self.save()

        def _later(key, why):
            """Перепроверка №2: временный сбой (нет/устарела серверная цена, суд недоступен) —
            не брак: повтор через час, заметка в дозор одна на версию. Окно ограничено само:
            через MAX_SEAL_LAG_DAYS шлагбаум скажет «старше 3 дней» — это уже брак."""
            import datetime as _dt
            first = key not in retry
            retry[key] = (now + _dt.timedelta(minutes=60)).isoformat(timespec="seconds")
            self.save()
            if first and why:
                self._notice_dozor(why)

        import datetime as _dt
        import hashlib
        import json as _json
        def _version(p, text):
            """Версия = текст + КАРТОЧКА (перепроверка №2: исправление одной карточки — новая версия)."""
            cp = HS.card_path_for(p)
            try:
                card_raw = cp.read_bytes().decode("utf-8", "replace") if cp.exists() else ""
            except OSError:
                card_raw = ""
            return p.name + "@" + hashlib.sha1((text + "\0" + card_raw).encode("utf-8")).hexdigest()[:10], card_raw

        def _one(p):
            try:
                text = p.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            key, card_raw = _version(p, text)
            # Ревью 02.11 (перепроверка): легаси-отметка «просто имя» (код до хеш-дедупа помечал так
            # и доставку, и брак — неразличимо) = файл уже обработан старым кодом: НЕ пересылаем
            # (иначе непроверенная судом записка ушла бы «обновлённой версией»).
            if p.name in seen or any(key + suf in seen for suf in ("", ":bad")):
                return None
            nxt = retry.get(key)
            if nxt:
                try:
                    if now < _dt.datetime.fromisoformat(nxt):
                        return None                        # временный сбой — ждём часа повтора
                except ValueError:
                    pass
            # «Обновлённая версия» — только для файла, доставленного НОВЫМ кодом (явный список)
            update = any(k.startswith(p.name + "@") for k in delivered)
            # П8: рядом обязано лежать исследование с источниками (*.md с URL, не пустышка)
            md = p.with_name(p.name[:-len(".push.txt")] + ".md")
            try:
                md_text = md.read_text(encoding="utf-8")
            except OSError:
                md_text = ""
            if len(md_text) < 200 or "http" not in md_text:
                if key + ":nomd" not in seen:          # заметка одна; появится .md — идея пойдёт
                    _mark(key + ":nomd")
                    self._notice_dozor("Идея охотника " + p.name + " без файла исследования с "
                                       "источниками (*.md с URL рядом, П8) — в Telegram не ушла.")
                return None
            # перепроверка №5: линтуется ИТОГОВЫЙ текст (после подстановки абзацев кода) в prepare_push —
            # тот же, что видит облачный предпросмотр; черновик сам по себе не бракуется
            # НОВАЯ идея: §9-карточка → шлагбаум КОДОМ на сервере (серверная дата и цены) → суд РАЗНЫХ
            # семейств (П10) → печать §9 (П16, обязательна) → доставка. ИСПРАВЛЕНИЕ уже доставленной
            # идеи: карточка → сверка со ЗАПЕЧАТАННОЙ ставкой → суд, без новой ставки. Кэш по версии.
            if key not in footers:
                card, asset = None, None
                if card_raw:
                    try:
                        card = _json.loads(card_raw)
                        _s9 = card.get("spec9") if isinstance(card, dict) and isinstance(card.get("spec9"), dict) else {}
                        asset = str(_s9.get("asset") or (card.get("ticker") if isinstance(card, dict) else "")
                                    or "").upper() or None
                    except ValueError as e:
                        card = None
                        log("карточка охотника не читается", p.name, repr(e))
                extra = []
                if not isinstance(card, dict):
                    if key + ":nocard" not in seen:        # заметка одна; появится карточка — пойдёт
                        _mark(key + ":nocard")
                        self._notice_dozor("Идея охотника " + p.name + " без §9-карточки (*.card.json) — "
                                           "без проверяемого прогноза идея не уходит.")
                    return None
                if update:
                    # исправление не вправе менять запечатанную ставку (перепроверка №2): иначе футер
                    # «уже записан» врёт, а владелец видит цель/срок не те, что в журнале
                    um = HS.update_bet_mismatch(card, p.name)
                    if um:
                        _mark(key + ":bad")
                        self._notice_owner("Исправление идеи охотника " + p.name + " не ушло: " + um
                                           + ". Новая ставка = новая идея с новой датой.")
                        return None
                    # перепроверка №4: шлагбаума новой ставки у исправления нет (честное «выхожу» после
                    # ухода цены он бы не пропустил), но цифры владельцу — по СЕРВЕРНЫМ ценам
                    card, rfails = HS.reprice(card)
                    if rfails:
                        if HS.is_transient(rfails):
                            _later(key, "Исправление идеи охотника " + p.name + " ждёт серверных котировок ("
                                   + str(rfails[0])[len(HS.TRANSIENT):][:160] + ") — повторю через час.")
                            return None
                        _mark(key + ":bad")
                        self._notice_owner("Исправление идеи охотника " + p.name + " не ушло: "
                                           + "; ".join(str(f) for f in rfails[:2]) + ".")
                        return None
                else:
                    # шлагбаум (окно катализатора, горизонт, якорь, «задним числом») — про НОВУЮ ставку
                    ok, gfails, card = HS.server_check(card, idea_file=p.name)
                    if not ok:
                        if HS.is_transient(gfails):
                            _later(key, "Идея охотника " + p.name + " ждёт серверных котировок ("
                                   + str(gfails[0])[len(HS.TRANSIENT):][:160] + ") — повторю через час.")
                            return None
                        _mark(key + ":bad")
                        self._notice_dozor("Идея охотника " + p.name + " не прошла серверный шлагбаум ("
                                           + "; ".join(str(f) for f in gfails[:2]) + ") — в Telegram не ушла.")
                        return None
                # перепроверка №5: ИТОГОВЫЙ текст собирается и проверяется ДО суда и ДО печати — сверка с
                # карточкой и линт по тому, что реально уйдёт владельцу (одна функция с облачным preview)
                final, pproblems = HS.prepare_push(text, card, update=update)
                if pproblems:
                    _mark(key + ":bad")
                    # ре-гейт 03.11: срыв ИСПРАВЛЕНИЯ уже доставленной идеи владелец видит (сторож его не ловит)
                    (self._notice_owner if update else self._notice_dozor)("Идея охотника " + p.name + " не прошла проверку итогового текста ("
                                       + "; ".join(str(x) for x in pproblems[:2]) + ") — в Telegram не ушла.")
                    return None
                ckey = p.name + "#" + hashlib.sha1(_json.dumps(card, sort_keys=True, ensure_ascii=False)
                                                   .encode("utf-8")).hexdigest()[:10]
                verdict = self._hunter_court(ckey, card)    # кэш по СОДЕРЖИМОМУ карточки, не по тексту
                if verdict is None:
                    _later(key, None)                   # суд недоступен — ждём (заметку пишет суд)
                    return None                            # непроверенное не шлём
                if verdict.get("verdict") == "FATAL":
                    if not update:
                        HS.seal_card(card, idea_file=p.name, kind=HS.SHADOW_KIND)
                    _mark(key + ":bad")
                    # перепроверка №3: заметки дозора уходят владельцу — только очищенное возражение
                    why_txt = HC.safe_objection(verdict) or ("пункт рубрики " + str(verdict.get("point") or "?")
                                                             + "; дословно — в кэше суда на сервере")
                    (self._notice_owner if update else self._notice_dozor)("Идею охотника " + p.name + " отверг суд разных семейств ("
                                       + why_txt + ") — в Telegram не ушла"
                                       + ("" if update else ", записана тенью для проверки права вето") + ".")
                    return None
                objection = None
                if verdict.get("verdict") == "FIX":
                    extra.append(HC.second_check_line(verdict) or "")
                    objection = HC.safe_objection(verdict)    # перепроверка №3: «Качаем» — тот же фильтр
                if update:
                    rec, why = None, ("уже запечатано" if HS.idea_sealed(p.name) else
                                      "это исправление уже отправленной записки — новой ставки не делаю")
                else:
                    rec, why = HS.seal_card(card, idea_file=p.name)
                    if rec is None and why != "уже запечатано":
                        # П16: новая идея без печати не уходит (порядок «печать до доставки»)
                        _mark(key + ":bad")
                        self._notice_dozor("Идея охотника " + p.name + ": прогноз не запечатан ("
                                           + str(why) + ") — без записи в счёт идея не уходит.")
                        return None
                    if rec is None:
                        # перепроверка №3: запечатано прошлой версией (доставка тогда сорвалась) — эта
                        # версия вправе уйти только с ТОЙ ЖЕ ставкой, иначе «уже записан» соврёт
                        um = HS.update_bet_mismatch(card, p.name)
                        if um:
                            _mark(key + ":bad")
                            self._notice_dozor("Новая версия идеи охотника " + p.name + " не ушла: " + um
                                               + ". Новая ставка = новая идея с новой датой.")
                            return None
                blocks = [b for b in final.split("\n\n")]
                footers[key] = {"lines": [x for x in extra if x] + HS.footer(rec, why, HS.method_score()),
                                "asset": asset, "sealed": bool(rec) or why == "уже запечатано",
                                "objection": objection, "text": final,
                                "name": str(card.get("name") or ""),
                                "catalyst_date": str((card.get("catalyst") or {}).get("date") or "")[:10]
                                if isinstance(card.get("catalyst"), dict) else "",
                                "header": blocks[0] if blocks else "",
                                "todo": next((b for b in blocks if b.strip().startswith("Что делать")), "")}
                retry.pop(key, None)
                self.save()
            foot = footers[key]
            # без карточки тикер берём из имени файла idea_<дата>_<ТИКЕР>.push.txt
            _stem_parts = p.name[len("idea_"):-len(".push.txt")].split("_")
            asset = foot.get("asset") or ((_stem_parts[1] + ".US") if len(_stem_parts) > 1 else None)
            lines = str(foot.get("text") or text).split("\n")
            frame_i = max((i for i, ln in enumerate(lines) if PL.RESEARCH_FRAME in ln),
                          default=len(lines))
            body = lines[:frame_i] + [""] + list(foot["lines"]) + lines[frame_i:]
            if update:
                # 03.11 (отзыв владельца: та же идея второй раз без новостей — шум): исправление уходит,
                # только если автор назвал, что изменилось по сути (абзац «Что изменилось.»); иначе
                # версия молча заменяет прошлую в состоянии бота, владельцу не шлётся
                changed = next((b for b in "\n".join(lines).split("\n\n")
                                if b.strip().lower().startswith("что изменилось")), None)
                if not changed:
                    # ре-гейт 03.11 (П8): тихая замена обязана обновить состояние идеи — иначе напоминание
                    # в день развилки сработало бы по СТАРОЙ дате и цитировало старый план и возражение
                    rid0 = "h_" + p.name[len("idea_"):-len(".push.txt")][:40]
                    lid0 = (self.state.get("hunter_lids") or {}).get(rid0)
                    if lid0 is not None:
                        for k in ("name", "catalyst_date", "header", "todo"):
                            if foot.get(k):
                                lid0[k] = foot[k]
                        if foot.get("objection"):
                            lid0["court_objection"] = foot["objection"]
                        self.state["hunter_lids"][rid0] = lid0
                        if (self.state.get("last_idea_day") or {}).get("run_id") == rid0:
                            self.state["last_idea_day"] = lid0
                    delivered.add(key)
                    self.state["hunter_delivered"] = sorted(delivered)
                    _mark(key)
                    log("исправление без «Что изменилось» — тихо заменено:", p.name)
                    return None
                body = ["Обновлённая версия записки.", ""] + body
            out = "\n".join(body)
            rid = "h_" + p.name[len("idea_"):-len(".push.txt")][:40]
            lids = self.state.setdefault("hunter_lids", {})
            prev = lids.get(rid) or {}
            # перепроверка №2: у каждой идеи охотника — свои кнопки (решение одно на идею): исправление
            # не воскрешает уже нажатые и не отнимает кнопки у текущей идеи дня
            kb = None if prev.get("action") else self._idea_kb(rid)
            _mid = self._send_card(out, kb)
            if _mid is None:
                return "stop"                                  # сеть — ретрай; доставленные уже отмечены
            self._log_delivery("обновление идеи" if update else "идея охотника", ref=p.name, text=out,
                               mid=_mid, buttons=kb is not None, lint_ok=True)
            lid = prev if update and prev else {
                "run_id": rid, "lead": asset, "idx": {},
                "issued_at": S.now_utc().isoformat(timespec="seconds"), "source": "hunter"}
            lid["sealed"] = bool(foot.get("sealed")) or bool(lid.get("sealed"))
            if foot.get("objection"):
                lid["court_objection"] = foot["objection"]
            for k in ("name", "catalyst_date", "header", "todo"):
                if foot.get(k):
                    lid[k] = foot[k]
            lid["idea_file"] = str(p.relative_to(d)) if p.parent != d else p.name   # запасная — alt/…
            if p.parent != d:
                lid["backup"] = True
            lids[rid] = lid
            for old in sorted(lids)[:-30]:              # держим последние 30 идей
                lids.pop(old, None)
            cur = self.state.get("last_idea_day") or {}
            if not update or cur.get("run_id") == rid:
                self.state["last_idea_day"] = lid
            if not update:
                self.state["idea_day_delivered"] = S.now_utc().date().isoformat()
            delivered.add(key)
            self.state["hunter_delivered"] = sorted(delivered)
            _mark(key)
            log("идея охотника доставлена:", p.name)

        # аудит 03.11 (P0): ЗАПАСНАЯ идея дня — reports/ideas/alt/idea_<дата>_<ТИКЕР>.* (второй по силе
        # защищаемый кандидат охоты). Тем же путём (шлагбаум, итоговый текст, суд, печать), но ТОЛЬКО
        # если ни одна идея этой даты не доставлена и все лидеры окончательно отбракованы (или их нет).
        def _leaders_dead(day):
            if any(k.startswith(f"idea_{day}_") for k in delivered):
                return False
            for lp in sorted(d.glob(f"idea_{day}_*.push.txt")):
                try:
                    lkey = _version(lp, lp.read_text(encoding="utf-8").strip())[0]
                except (OSError, ValueError):
                    return False                     # не читается — не решаем за лидера
                if lkey + ":bad" in seen:
                    continue
                # ре-гейт 03.11: лидер без исследования (.md) или карточки дольше 3 часов после протяжки —
                # облако его уже не дочинит в этот день; запасная не должна ждать вечно
                stale = False
                try:
                    import time as _time
                    stale = (_time.time() - lp.stat().st_mtime) > 3 * 3600
                except OSError:
                    pass
                if stale and (lkey + ":nomd" in seen or lkey + ":nocard" in seen):
                    continue
                return False                         # лидер жив или ещё ждёт (котировки, суд, .md)
            return True

        _alts = []
        if (d / "alt").is_dir():
            for pa in sorted((d / "alt").glob("idea_*.push.txt")):
                day = pa.name[len("idea_"):len("idea_") + 10]
                if (d / pa.name).exists() or not _leaders_dead(day):
                    continue
                _alts.append(pa)

        for p in sorted(d.glob("idea_*.push.txt")) + _alts:
            if p.parent != d:
                _day = p.name[len("idea_"):len("idea_") + 10]
                if not _leaders_dead(_day):          # первая запасная этой даты уже ушла в этом же тике
                    continue
            try:
                if _one(p) == "stop":
                    return                              # сеть — ретрай; доставленные уже отмечены
            except Exception as e:                      # noqa: BLE001 — перепроверка №4: одна битая
                # карточка роняла весь тик и навсегда блокировала все следующие идеи
                log("идея охотника: сбой обработки", p.name, repr(e))
                import sqlite3 as _sq
                try:   # перепроверка №6: битый UTF-8 — ValueError; читаем байты с заменой, ключ всегда есть
                    vkey = _version(p, p.read_bytes().decode("utf-8", "replace").strip())[0]
                except OSError:
                    vkey = None
                if isinstance(e, (OSError, _sq.Error, TimeoutError, ConnectionError)) and vkey:
                    # перепроверка №5: сбой окружения (диск, лок журнала, база, сеть) — не брак: повтор
                    _later(vkey, "Идея охотника " + p.name + " отложена из-за сбоя окружения ("
                           + type(e).__name__ + ") — повторю через час.")
                    continue
                bad = (vkey or p.name + "@?") + ":bad"
                if bad not in seen:
                    _mark(bad)
                    self._notice_dozor("Идея охотника " + p.name + " не обработана (" + type(e).__name__
                                       + ") — в Telegram не ушла; остальные идеи идут дальше.")

    def _hunter_court(self, key, card):
        """Суд разных семейств (П10) с кэшем по версии идеи и паузой ретрая 60 мин при
        недоступности (fail-closed: непроверенное не шлём; заметка в дозор — один раз)."""
        try:
            from ops import hunter_court as HC
        except ImportError:
            import hunter_court as HC
        cache = self.state.setdefault("hunter_court", {})
        got = cache.get(key) or {}
        if got.get("v"):
            return got["v"]
        now = S.now_utc()
        last = got.get("tried_at")
        if last:
            try:
                import datetime as _dt
                if (now - _dt.datetime.fromisoformat(last)).total_seconds() < 3600:
                    return None
            except ValueError:
                pass
        v, why = HC.judge_card(card, run_id="hunter_court")
        cache[key] = {"v": v, "why": why, "tried_at": now.isoformat(timespec="seconds"),
                      "noted": got.get("noted")}
        if v is None and not got.get("noted"):
            cache[key]["noted"] = True
            self._notice_dozor("Идея охотника готова, но суд разных семейств сейчас недоступен ("
                               + str(why)[:160] + ") — непроверенное не отправляю, повторю через час.")
        self.save()
        return v

    def _tick_hunter_followups(self):
        """Охота v3: (1) ТЕНИ — кандидаты, отвергнутые судом охотника (idea_<дата>_*.shadow.json),
        печатаются в трек hunter_killed БЕЗ показа владельцу: «вето надо заработать» — через 20+
        сверенных видно, отличает ли суд плохое от хорошего; (2) ИТОГИ — когда резолвер сверил
        идею охотника, владелец получает «сбылось / не сбылось» и счёт метода (обещание пуша)."""
        if not R.load_presentation().get("hunter_push"):
            return
        try:
            from ops import hunter_seal as HS
        except ImportError:
            import hunter_seal as HS
        import json as _json
        d = self._ideas_dir()
        done = set(self.state.get("hunter_shadow") or [])
        sretry = self.state.setdefault("hunter_shadow_retry", {})
        import datetime as _dt
        _now = S.now_utc()
        if d.exists():
            for p in sorted(d.glob("idea_*.shadow.json")):
                if p.name in done:
                    continue
                try:
                    if p.name in sretry and _now < _dt.datetime.fromisoformat(sretry[p.name]):
                        continue                        # сбой данных — повтор не чаще раза в час
                except ValueError:
                    pass
                try:
                    rows = _json.loads(p.read_bytes().decode("utf-8", "replace"))
                except OSError:                         # прицельная 03.11: сбой чтения — повтор, не потеря
                    sretry[p.name] = (_now + _dt.timedelta(minutes=60)).isoformat(timespec="seconds")
                    self.save()
                    continue
                except ValueError:
                    rows = []                           # битый JSON — данных нет
                rows = rows if isinstance(rows, list) else [rows]
                wait = False
                for card in rows:
                    if not isinstance(card, dict):
                        continue
                    try:
                        # перепроверка 02.11: печать «одна на идею» по стему теряла все тени, кроме
                        # первой — у каждой тени свой стем (имя файла + тикер)
                        _s9 = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
                        tk = str(card.get("ticker") or _s9.get("asset") or "?")
                        ok, _f, card2 = HS.server_check(card, idea_file=p.name)   # тени — тем же шлагбаумом
                        if ok:
                            HS.seal_card(card2, idea_file=f"{p.name}#{tk}", kind=HS.SHADOW_KIND)
                        elif HS.is_transient(_f):
                            wait = True                 # перепроверка №2: сбой данных — повторим
                    except Exception as e:              # noqa: BLE001 — перепроверка №5: одна битая тень
                        log("тень охотника: сбой", p.name, repr(e))   # не блокирует напоминания и итоги
                        import sqlite3 as _sq
                        if isinstance(e, (OSError, _sq.Error, TimeoutError, ConnectionError)):
                            wait = True                 # перепроверка №6: сбой окружения — повтор, тень не теряем
                if wait:                                # уже запечатанные — дубль «уже запечатано»
                    sretry[p.name] = (_now + _dt.timedelta(minutes=60)).isoformat(timespec="seconds")
                    self.save()
                    continue
                sretry.pop(p.name, None)
                done.add(p.name)
                self.state["hunter_shadow"] = sorted(done)
                self.save()
        self._tick_hunter_fork_reminders()
        sent = set(self.state.get("hunter_outcomes_sent") or [])
        fresh = [o for o in HS._read_jsonl(HS.OUTC)
                 if o.get("kind") == HS.KIND and o.get("hash") and o.get("outcome") in (0, 1)
                 and o["hash"] not in sent]
        if not fresh:
            return
        score = HS.method_score()
        for o in fresh:
            _txt = HS.outcome_message(o, score)
            _mid = self.tg.send_message(self.chat_id, _txt)
            if _mid is None:
                return                                   # сеть — ретрай следующим тиком
            self._log_delivery("итог сверки", ref=str(o.get("hash"))[:16], text=_txt, mid=_mid)
            sent.add(o["hash"])
            self.state["hunter_outcomes_sent"] = sorted(sent)
            self.save()

    def _tick_hunter_fork_reminders(self, now=None):
        """Перепроверка №4 (обещания без исполнителя): записка охотника называет день развилки
        (катализатор), а писать в этот день было некому. Теперь КОД сам напоминает в день
        катализатора (с 12:00 UTC — до открытия биржи в Нью-Йорке): условие из заголовка и план
        из «Что делать» — дословно из уже проверенного пуша, без новых чисел. Один раз на идею."""
        now = now or S.now_utc()
        if now.hour < 12:
            return
        today = now.date().isoformat()
        lids = self.state.get("hunter_lids") or {}
        for rid, lid in sorted(lids.items()):
            if lid.get("catalyst_date") != today or lid.get("fork_reminded") == today:
                continue
            if not lid.get("sealed") or lid.get("action") == "skip":
                continue                                 # «Мимо» — не напоминаем
            base = str(lid.get("lead") or "").split(".")[0]
            parts = [f"Сегодня день развилки по идее {lid.get('name') or base} ({base}) — напоминание "
                     "кода по дате из записки."]
            if lid.get("header"):
                parts.append("Условие из записки: «" + str(lid["header"]).strip() + "»")
            if lid.get("todo"):
                parts.append("План: «" + str(lid["todo"]).strip() + "»")
            parts.append("Выполнилось ли условие — по первоисточникам из исследования; «Качаем» под "
                         "запиской покажет, на чём стоит идея. Это исследовательский инструмент, "
                         "не инвестиционная рекомендация.")
            _txt = "\n\n".join(parts)
            _mid = self.tg.send_message(self.chat_id, _txt)
            if _mid is None:
                return
            self._log_delivery("напоминание развилки", ref=rid, text=_txt, mid=_mid)
            lid["fork_reminded"] = today
            lids[rid] = lid
            self.state["hunter_lids"] = lids
            self.save()

    def _ideas_dir(self):
        """Аудит 03.11 (охранный пакет): при отказе протяжки ops/safe_pull.sh кладёт снимок
        reports/ideas/ из GitHub в .cache/ideas_incoming/ — он новее рабочей копии (эту папку пишет
        только облако), и бот читает идеи оттуда: «заморозка» кода не останавливает идею дня."""
        inc = ROOT / ".cache" / "ideas_incoming" / "reports" / "ideas"
        return inc if inc.is_dir() else ROOT / "reports" / "ideas"

    def _ideas_flow_note(self):
        """Правда о доставке идей при отказе протяжки: снимок есть — идеи идут, нет — стоят тоже."""
        if (ROOT / ".cache" / "ideas_incoming" / "reports" / "ideas").is_dir():
            return "идеи охотника при этом доходят отдельно — снимком из GitHub"
        return "идеи охотника тоже не доходят, пока протяжка не пройдёт"

    def _delay_reason(self, day):
        """Причина задержки идеи дня одной человеческой фразой — по служебным заметкам дозора за день
        (аудит 03.11, P1 №13: одно сообщение владельцу вместо человеческого + технического)."""
        import json as _json
        try:
            lines = (ROOT / "journal" / "notices.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        last = ""
        for ln in lines:
            try:
                r = _json.loads(ln)
            except ValueError:
                continue
            t = str(r.get("text") or "")
            if r.get("silent") and str(r.get("ts") or "")[:10] == day and "охотника" in t:
                last = t
        rules = (("ждёт серверных котировок", "Сервер ещё не получил свежие цены, чтобы пересчитать её цифры."),
                 ("суд разных семейств сейчас недоступен", "Вторая проверка (модели других разработчиков) сейчас недоступна — непроверенное не шлю."),
                 ("отверг суд", "Вторая проверка нашла улику против идеи — такую не защищаю."),
                 ("серверный шлагбаум", "Сервер пересчитал цены по своим котировкам, и идея перестала проходить планку."),
                 ("итогового текста", "Текст записки не прошёл проверку подачи."),
                 ("не запечатан", "Прогноз не удалось записать в счёт — без записи идею не шлю."),
                 ("без файла исследования", "Не пришло исследование с источниками — без него идею не шлю."))
        for key, why in rules:
            if key in last:
                return why
        return "Охотник её ещё не прислал — поиск или проверка не закончены."

    def _honest_zero_text(self, day):
        """Честный ноль охотника (reports/ideas/idea_<дата>_none.json): почему сегодня нет идеи, которую
        система готова защищать, и при каком условии ближайший кандидат станет идеей. Текст модели —
        под линтом (вероятности, тех.id, тикеры без имён, жаргон); не прошёл — None (общий текст сторожа)."""
        import json as _json
        try:
            from ops import presentation_lint as PL
        except ImportError:
            import presentation_lint as PL
        f = self._ideas_dir() / f"idea_{day}_none.json"
        try:
            rec = _json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(rec, dict):
            return None
        reasons = [" ".join(str(x).split()) for x in (rec.get("reasons") or []) if str(x).strip()][:2]
        if not reasons:
            return None
        parts = ["Сегодня идеи, которую я готов защищать, нет — говорю честно, а не выдаю слабую."]
        n = rec.get("checked")
        parts.append((f"Проверил кандидатов: {int(n)}. " if isinstance(n, int) and n > 0 else "")
                     + "Почему не прошли: " + "; ".join(r.rstrip(".") for r in reasons) + ".")
        bc = rec.get("best_candidate") if isinstance(rec.get("best_candidate"), dict) else {}
        if bc.get("name") and bc.get("becomes_idea_if"):
            tk = str(bc.get("ticker") or "").split(".")[0]
            who = f"{bc['name']} ({tk})" if tk else str(bc["name"])
            parts.append(f"Ближе всех — {who}: " + " ".join(str(bc.get("what_missing") or "").split()).rstrip(".")
                         + (". " if bc.get("what_missing") else "") + "Станет идеей, если "
                         + " ".join(str(bc["becomes_idea_if"]).split()).rstrip(".") + ".")
        parts.append("Завтра охотник ищет снова. Это " + PL.RESEARCH_FRAME + ".")
        text = "\n\n".join(parts)
        viol = PL.lint_for_user(text, max_len=1500)
        if PL.odds_markers(text, include_quotes=True):
            viol.append("вероятности в честном ноле")
        if PL._TECH_ID_RE.search(text) or PL._bare_tickers(text):
            viol.append("тех.id или тикер без имени")
        if viol:
            self._notice_dozor("Честный ноль охотника за " + day + " не прошёл проверку подачи ("
                               + "; ".join(str(v) for v in viol[:2]) + ") — ушло общее сообщение сторожа.")
            return None
        return text

    def _tick_idea_day_watchdog(self, now=None):
        """Ревью 02.11 (тихий день): утреннее «наблюдение» сервера глушится в пользу идеи охотника —
        если к 13:00 UTC идея дня так и не доставлена, владелец получает ОДНО честное сообщение
        (без «помойки»), что идея задерживается и почему, а дозор — заметку."""
        if R.load_presentation().get("server_observation_push", True):
            return
        now = now or S.now_utc()
        today = now.date().isoformat()
        if self.state.get("idea_day_delivered") == today or self.state.get("idea_day_watchdog") == today:
            return
        # ре-гейт 03.11: честный ноль — СРАЗУ, как охотник его положил (а не в час сторожа): владелец
        # узнаёт утром, и замер точки Б (доставка до 15:00 UTC) его засчитывает
        zero = self._honest_zero_text(today)
        if not zero and now.hour < WATCHDOG_HOUR_UTC:
            return
        if zero:                                 # аудит 03.11: честный ноль охотника — и в выходные тоже
            _mid = self.tg.send_message(self.chat_id, zero)
            if _mid is None:
                return
            self._log_delivery("честный ноль", ref=f"idea_{today}_none.json", text=zero, mid=_mid, lint_ok=True)
            self.state["idea_day_watchdog"] = today
            self.save()
            return
        if now.isoweekday() > 5:                 # выходные: охотник работает, но молчание не аварийно
            return
        # перепроверка №2: без обещаний, которые бот не контролирует (идея могла и не пройти)
        msg = ("Идея дня сегодня пока не готова. " + self._delay_reason(today) + " Если пройдёт "
               "сегодня — пришлю сразу.")
        _mid = self.tg.send_message(self.chat_id, msg)
        if _mid is None:
            return
        self._log_delivery("тихий день", ref=today, text=msg, mid=_mid)
        self.state["idea_day_watchdog"] = today
        self._notice_dozor(f"Сторож: к {WATCHDOG_HOUR_UTC}:00 UTC идея дня не доставлена — проверить охотника "
                           "(облачный прогон, коммит, git pull, шлагбаум/суд на сервере).")
        self.save()

    def _tick_proposal_batch(self, now=None):
        """Раз в неделю (воскресенье, после 10:00 UTC — суббота занята темами) — карточка
        предложений петли §25 (V2). Дедуп по ISO-неделе; пустая очередь — карточки нет."""
        import bot_proposals as PR
        now = now or S.now_utc()
        if now.isoweekday() != 7 or now.hour < 10:
            return
        week = now.strftime("%G-%V")
        if self.state.get("proposals_week") == week:
            return
        if self.state.get("proposals_pending"):   # прошлый пакет не разобран — не наслаиваем
            self.state["proposals_week"] = week
            return
        items = PR.pending()
        if not items:
            self.state["proposals_week"] = week
            return
        text = PR.format_card(items)
        kb = PR.build_keyboard(items)
        if self._send_card(text, kb) is None:
            return                                 # не дошло — попробуем следующим tick'ом
        self.state["proposals_week"] = week
        self.state["proposals_pending"] = {k: r for k, r in items}
        self.save()
        log("карточка предложений §25 отправлена:", len(items))

    def _tick_week_odds(self):
        """V5: пуш «Шансов недели» по новому протоколу journal/week_odds/wo_*.json.
        За флагом presentation week_odds_push (ПРАВИЛО ВНЕДРЕНИЯ 01.11: включается после
        ручного прогона испытателя и «ок» владельца). Fail-closed линт; дедуп по файлу."""
        pres = R.load_presentation()
        if not pres.get("week_odds_push"):
            return
        d = ROOT / "journal" / "week_odds"
        if not d.exists():
            return
        files = sorted(d.glob("wo_*.json"))
        if not files:
            return
        sent = self.state.setdefault("week_odds_sent", [])
        import json as _json
        latest, proto = None, None
        # ре-гейт 03.11: водяной знак (имена wo_<время>.json растут) — список sent обрезается до 50,
        # и без знака после 51-го протокола старые выпуски уходили бы владельцу снова и снова
        mark = self.state.get("week_odds_mark") or (max(sent) if sent else "")
        for f in files:
            if f.name in sent or f.name <= mark:
                continue
            self.state["week_odds_mark"] = max(self.state.get("week_odds_mark") or "", f.name)
            try:
                pr = _json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                sent.append(f.name)
                continue
            # аудит 03.11: ручной показ (--show без --seal) — не сбой крона; молча мимо. Протоколы до
            # 03.11 поля не знают (ре-гейт: 2 сухих прогона 03.11 дали бы ложную тревогу) — тоже сухие
            if not pr.get("печать_запрошена"):
                sent.append(f.name)
                log("Шансы недели: сухой прогон без печати — не пушим", f.name)
                continue
            # Ре-гейт V0 02.11 (П8): mock/без печатей владельцу НЕ уходит — шаблон говорит
            # «каждый вопрос запечатан». Печать была запрошена, а её нет — аномалия, в дозор.
            if pr.get("mode") == "mock" or not pr.get("запечатано"):
                sent.append(f.name)
                log("Шансы недели: протокол без печатей (mode/запечатано) — пуш пропущен", f.name)
                self._notice_owner("«Шансы недели» (" + f.name + "): протокол без печатей "
                                   "(mode=" + str(pr.get("mode")) + ", запечатано="
                                   + str(pr.get("запечатано")) + ") — пуш пропущен. Проверить крон/ключ.")
                continue
            if latest is not None:
                sent.append(latest.name)              # старший запечатанный вытеснен свежим
            latest, proto = f, pr
        if latest is None:
            self.state["week_odds_sent"] = sent[-50:]
            return
        try:                                   # счёт взглядов из журнала (П16: не из файла)
            import forward_score as FS
            _scores = FS.view_scores()
        except Exception:                      # noqa: BLE001 — подача живёт и без счёта
            _scores = None
        try:                                   # счёт трека из журнала (аудит 03.11: не константа)
            import point_b as PB
            _track = PB.track_skill("thesis_event", PB._jsonl(PB.PRED), PB._jsonl(PB.OUTC))
        except Exception:                      # noqa: BLE001
            _track = None
        text, viol = R.format_week_odds(proto, scores=_scores, track=_track)
        if text is None:
            # Ре-гейт V0 02.11: молчание — нарушение правила этапа; заметка в дозор как у V0
            log("Шансы недели: пуш не ушёл", latest.name, viol[:2])
            self._notice_owner("«Шансы недели» (" + latest.name + ") не прошли проверку подачи ("
                               + "; ".join(str(v) for v in (viol or [])[:2]) + ") — пуш не ушёл, "
                               "а вопросы уже запечатаны. Нужен разбор машинистом.")
            sent.append(latest.name)          # битый/пустой протокол не ретраим вечно
            return
        _mid = self._send_card(text, None)
        if _mid is None:
            return                             # сеть — ретрай следующим tick
        self._log_delivery("Шансы недели", ref=latest.name, text=text, mid=_mid, lint_ok=True)
        sent.append(latest.name)
        self.state["week_odds_sent"] = sent[-50:]
        log("пуш «Шансы недели»", latest.name)

    def _tick_theme_batch(self, now=None):
        """Раз в неделю (суббота, после 10:00 UTC) — карточка «утверди темы» (V2, подпись П5).
        Дедуп по ISO-неделе; без неразобранных тем карточка не шлётся."""
        import bot_themes as TH
        now = now or S.now_utc()
        if now.isoweekday() != 6 or now.hour < 10:
            return
        week = now.strftime("%G-%V")
        if self.state.get("themes_week") == week:
            return
        # аудит 03.11: неотвеченные темы прошлой недели не блокируют новую карточку — они просто
        # возвращаются в очередь (решения по ним нет), а старые кнопки гаснут
        self.state["themes_pending"] = {}
        items = TH.pending_queue()
        if not items:
            self.state["themes_week"] = week
            return
        text = TH.format_card(items)
        kb = TH.build_keyboard(items)
        if self._send_card(text, kb) is None:
            return                                 # не дошло — попробуем следующим tick'ом
        self.state["themes_week"] = week
        self.state["themes_pending"] = {k: r for k, r in items}
        self.save()
        log("карточка тем недели отправлена:", len(items))

    @staticmethod
    def _intent_more_ideas(text):
        """V0: «что ещё было» / «ещё идеи» / «остальные идеи» — показать остальные идеи дня списком."""
        t = (text or "").lower().strip().strip(".!?…")
        if len(t) > 40:
            return False
        # узко (stage-review V0): голое «что ещё» перехватывало бы обычные вопросы владельца
        return any(k in t for k in ("что ещё было", "что еще было", "ещё идеи", "еще идеи",
                                    "остальные идеи", "другие идеи"))

    def _cmd_more_ideas(self, chat):
        """Остальные идеи дня = «Топ-3» того же прогона, из готового суда, через линт стиль-контракта."""
        lid = self.state.get("last_idea_day") or self.state.get("last_top3")
        # аудит 03.11: под идеей охотника «что ещё было» = кандидаты ЭТОЙ охоты (hunt_log.jsonl),
        # а не Топ-3 старого конвейера; журнала за дату нет — прежний фолбэк на Топ-3 сервера
        if lid and str(lid.get("run_id") or "").startswith("h_"):
            text = self._hunter_more_text(lid)
            if text:
                self._send_card(text, None)
                return
            lid = self.state.get("last_top3")
        if not lid or not lid.get("run_id"):
            self.tg.send_message(chat, "Сегодняшней подборки у меня нет — скажи «найди идеи», сделаю прогон.")
            return
        proto = R.load_protocol(lid["run_id"])
        if not proto:
            self.tg.send_message(chat, "Не нашёл протокол того прогона — сделай свежий («найди идеи»).")
            return
        text, meta, viol = R.top3_from_protocol(proto)
        if text is None:
            log("«что ещё было»: Топ-3 не прошёл стиль-контракт", lid["run_id"], viol)
            self.tg.send_message(chat, "Список идей не прошёл проверку подачи — покажу по одной: "
                                       "напиши «разбери ТИКЕР».")
            return
        self._send_card(text, None)
        self.state["last_top3"] = {"run_id": lid["run_id"], "idx": (meta or {}).get("idx") or {}}
        self.save()

    def _hunter_more_text(self, lid):
        """Кандидаты той же охоты, не ставшие идеей дня, — человеческим языком (без линз и FIX/PASS):
        имя и роль из журнала охоты, причина — по вердикту, «вернусь, если» — только если чистое."""
        import json as _json
        import re as _re
        try:
            from ops import presentation_lint as PL
        except ImportError:
            import presentation_lint as PL
        day = str(lid.get("run_id"))[2:12]
        lead = str(lid.get("lead") or "").split(".")[0].upper()
        try:
            lines = (self._ideas_dir() / "hunt_log.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        why_ru = {"отвергнута": "проверка нашла улику против идеи",
                  "не доведена": "не все опорные факты удалось подтвердить первоисточниками за время поиска",
                  "запасная": "запасная идея дня — была бы отправлена, если бы главная не прошла проверку"}
        rows, seen = [], set()
        for ln in lines:
            try:
                r = _json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict) or str(r.get("дата")) != day:
                continue
            inst = " ".join(str(r.get("инструмент") or "").split())
            m = _re.match(r"^([A-Z0-9.]{1,10})\s*\((.+)\)\s*$", inst)
            tk, about = (m.group(1).split(".")[0], m.group(2)) if m else (inst.split(" ")[0].split(".")[0], "")
            if not tk or tk == lead or tk in seen or r.get("вердикт") == "выбрана":
                continue
            seen.add(tk)
            parts = [x.strip() for x in about.split(" — ", 1)]
            name = parts[0] or tk
            role = parts[1] if len(parts) > 1 else ""
            if len(role) > 110:
                role = role[:110].rsplit(" ", 1)[0].rstrip(",;") + "…"
            # условия «пересмотреть, если» охотник пишет для себя (URL, гарды, коридоры цен) — владельцу
            # не показываем: роль компании и причина простыми словами полезнее
            line = (f"• {name} ({tk})" + (f", {role}" if role else "") + ": "
                    + why_ru.get(str(r.get("вердикт")), "не стала идеей дня") + ".")
            if PL.odds_markers(line, include_quotes=True):
                line = f"• {name} ({tk}): " + why_ru.get(str(r.get("вердикт")), "не стала идеей дня") + "."
            rows.append(line)
        if not rows:
            return None
        text = ("Что ещё смотрел охотник в этот день — кандидаты, которые не стали идеей дня:\n\n"
                + "\n".join(rows[:6]) + "\n\nЛюбую можно разобрать подробно: «разбери ТИКЕР». Это "
                + PL.RESEARCH_FRAME + ".")
        viol = PL.lint_for_user(text, max_len=3500)
        if viol or PL._bare_tickers(text) or PL._TECH_ID_RE.search(text):
            log("«что ещё было» охотника не прошло подачу", viol[:2])
            return None
        return text

    def _log_delivery(self, kind, ref=None, text=None, mid=None, buttons=False, lint_ok=None):
        """Аудит 03.11 (P0): журнал доставок journal/deliveries.jsonl (append-only, не §16) — что
        РЕАЛЬНО ушло владельцу: вид, ссылка (файл/id), sha256 текста, кнопки, message_id. По нему
        считаются «дни с идеей» точки Б и здоровье доставки у машиниста. Сбой записи не роняет пуш."""
        import json as _json
        import hashlib as _hl
        rec = {"ts": S.now_utc().isoformat(timespec="seconds"), "kind": kind, "ref": ref,
               "sha256": _hl.sha256(text.encode("utf-8")).hexdigest() if text else None,
               "chars": len(text) if text else 0, "buttons": bool(buttons), "lint_ok": lint_ok,
               "message_id": mid}
        try:
            path = DELIVERIES_PATH or (ROOT / "journal" / "deliveries.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log("журнал доставок не записан", repr(e))
        return rec

    def _tick_scheduled_jobs(self, now=None, popen=None):
        """Задачи за флагами (SCHEDULED_JOBS): раз в свой день после своего часа, отдельным процессом
        (бот не замирает на минуты LLM-вызовов) через ops/cron_alert.sh — падение уходит алертом,
        вывод — в journal/cron.log. Уже идущая задача второй раз не стартует."""
        import subprocess
        now = now or S.now_utc()
        pres = R.load_presentation()
        procs = getattr(self, "_job_procs", {})
        self._job_procs = procs
        for label, pr in list(procs.items()):
            if pr.poll() is not None:                    # закончилась — прибираем
                procs.pop(label, None)
        days = self.state.setdefault("jobs_last_day", {})
        today = now.date().isoformat()
        for flag, label, argv, weekdays, (hh, mm) in SCHEDULED_JOBS:
            if not pres.get(flag) or now.isoweekday() not in weekdays or (now.hour, now.minute) < (hh, mm):
                continue
            if days.get(label) == today or label in procs:
                continue
            cmd = [str(ROOT / "ops" / "cron_alert.sh"), label, sys.executable, *argv]
            try:
                with open(ROOT / "journal" / "cron.log", "a", encoding="utf-8") as logf:   # ребёнок держит свою копию
                    procs[label] = (popen or subprocess.Popen)(cmd, cwd=str(ROOT), stdout=logf, stderr=logf,
                                                               start_new_session=True)
            except OSError as e:
                log("задача за флагом не запустилась", label, repr(e))
                continue
            days[label] = today
            log("запущена задача за флагом", flag, label)
            self.save()

    def _tick_heartbeat(self, now=None):
        """Суточная строка «бот жив» в журнале доставок — тишина в нём = бот лежит (не «идей нет»)."""
        today = (now or S.now_utc()).date().isoformat()
        if self.state.get("heartbeat_day") == today:
            return
        self._log_delivery("бот жив", ref=today)
        self.state["heartbeat_day"] = today

    def _notice_dozor(self, text):
        """Служебная заметка дозора (silent) — см. _write_notice."""
        self._write_notice(text, owner=False)

    def _write_notice(self, text, owner=False):
        """Заметка в дозор (journal/notices.jsonl). Аудит 03.11 (P1 №13, шум): по умолчанию СЛУЖЕБНАЯ —
        пишется с silent=true, её читают машинист/испытатель, а владельцу в Telegram не уходит (он
        получает одно человеческое сообщение сторожа с причиной). owner=True — редкое и важное для
        владельца (см. _notice_owner)."""
        import json as _json
        import datetime as _dt
        path = ROOT / "journal" / "notices.jsonl"
        rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "text": text}
        if not owner:
            rec["silent"] = True
            rec["source"] = "дозор"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log("заметка дозора не записана", repr(e))

    def _notice_owner(self, text):
        """Заметка, которую бот пушит ВЛАДЕЛЬЦУ (редкое и требующее его внимания)."""
        self._write_notice(text, owner=True)

    def _voice_text(self, chat, msg):
        """Голосовое сообщение → распознанный текст (или None + человеческое объяснение в чат).

        Гейты как у диалога: бюджет §11 (STT = LLM-вызов, пишется в costs.jsonl) и ключ
        OpenRouter. Распознаём СИНХРОННО (секунды, flash-модель) — фоновый поток не нужен,
        а текст должен попасть в тот же путь роутинга, что и печатный."""
        voice = msg.get("voice") or msg.get("audio") or {}
        import bot_voice as V
        if self._budget_blocked(chat, "распознавание голосовых"):
            return None
        if not os.environ.get("OPENROUTER_API_KEY"):
            self.tg.send_message(chat, "Нет ключа OpenRouter — распознать голосовое не могу. "
                                       "Напиши, пожалуйста, текстом.")
            return None
        dur = voice.get("duration") or 0
        if dur > V.MAX_VOICE_SEC:
            self.tg.send_message(chat, f"Голосовое длиннее {V.MAX_VOICE_SEC // 60} минут — такое "
                                       f"не распознаю (защита бюджета). Скажи короче или напиши текстом.")
            return None
        if hasattr(self.tg, "send_chat_action"):
            self.tg.send_chat_action(chat, "typing")
        try:
            text = V.voice_to_text(self.tg, voice)
        except Exception as e:                               # noqa: BLE001 — бот не падает из-за STT
            log("voice STT ошибка", repr(e))
            self.tg.send_message(chat, f"Не смог распознать голосовое ({e}). Напиши текстом, пожалуйста.")
            return None
        if not text:
            self.tg.send_message(chat, "Не разобрал слова в голосовом — запиши ещё раз или напиши текстом.")
            return None
        log("voice STT ок,", len(text), "симв")
        # Эхо распознанного — владелец видит, ЧТО именно я услышал (П8: не выдумываем команду).
        self.tg.send_message(chat, f"🎙 Услышал: «{text}»")
        return text

    @staticmethod
    def _intent_session(text):
        """П3: «дай идеи»/«сессия» = ПОКАЗАТЬ готовые идеи (не путать с «найди идеи» = ПРОГОН:
        поиск нового скана — дороже и дольше; показ — мгновенный, из последнего протокола)."""
        t = text.lower().strip().strip(".!?…")
        for pre in ("да, ", "да ", "ок, ", "ок ", "окей ", "хорошо, ", "хорошо ", "давай, ", "давай "):
            if t.startswith(pre):
                t = t[len(pre):].strip()
        return t in {"сессия", "сессия партнёра", "сессия партнера", "дай идеи", "идеи",
                     "идеи дня", "покажи идеи", "что по идеям", "дай идеи дня"}

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
                "🎙 Голосовые тоже понимаю: распознаю речь, покажу «что услышал» и выполню как "
                "обычное сообщение («дай идеи», «разбери GEV», вопрос словами).\n\n"
                "🚀 Можешь сам запустить прогон: напиши «/run-funnel» (или просто «сделай прогон» / "
                "«найди идеи») — я прогоню воронку (открытый скан мира → каскады в компании) и пришлю "
                "дайджест. Прогон идёт несколько минут; ход — в /progress. Может выйти и «идей нет» — "
                "это честный результат.\n\n"
                "🥊 Сомневаешься в идее? Напиши «/debate <твоё возражение>» — и я НЕ отвечу сам, "
                "а сведу спор разных моделей: одна защищает идею, другая атакует твоим возражением, "
                "третья (слепой судья) выносит вердикт — выдержала идея твоё сомнение или нет.\n\n"
                "Команды: /session (или просто «дай идеи») — сессия партнёра: 3-5 идей с готовой защитой из последнего прогона; /run-funnel — запустить прогон; /calibrate — калибровочный прогон (копим "
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
        elif cmd in ("session", "сессия"):
            self._cmd_session(chat)
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

    def _cmd_session(self, chat):
        """П3 (§17.6): сессия партнёра — 3-5 идей из ПОСЛЕДНЕГО прогона с готовой защитой.
        Детерминированно и мгновенно (LLM не зовётся); сессия журналируется (метрики §R5)."""
        import datetime as _dt
        from orchestrator import partner_session as PS
        asof = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        try:
            proto = PS.load_latest_protocol()
            session = PS.build_session(proto, asof=asof)
            if not session.get("ОТКАЗ"):
                PS.record_session(session)
            metrics = PS.session_metrics(asof=asof)
            self._send_card(R.format_partner_session(session, metrics), None)
            log("сессия партнёра", session.get("run_id_источника"), "идей", session.get("идей"))
        except Exception as e:  # noqa: BLE001 — сессия не должна ронять бота
            log("сессия партнёра ошибка", repr(e))
            self.tg.send_message(chat, f"Не собрал сессию: {e}. Попробуй /run-funnel — соберу свежий прогон.")

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
        try:
            self._tick_alerts()
        except Exception as e:                                   # noqa: BLE001
            log("tick alerts ошибка", repr(e))
        try:
            self._tick_notices()
        except Exception as e:                                   # noqa: BLE001
            log("tick notices ошибка", repr(e))
        try:
            self._tick_theme_batch(now)
        except Exception as e:                                   # noqa: BLE001
            log("tick themes ошибка", repr(e))
        try:
            self._tick_proposal_batch(now)
        except Exception as e:                                   # noqa: BLE001
            log("tick proposals ошибка", repr(e))
        try:
            self._tick_hunter_ideas()
        except Exception as e:                                   # noqa: BLE001
            log("tick hunter ошибка", repr(e))
        try:
            self._tick_hunter_followups()
        except Exception as e:                                   # noqa: BLE001
            log("tick hunter followups ошибка", repr(e))
        try:
            self._tick_idea_day_watchdog(now)
        except Exception as e:                                   # noqa: BLE001
            log("tick watchdog ошибка", repr(e))
        try:
            self._tick_week_odds()
        except Exception as e:                                   # noqa: BLE001
            log("tick week_odds ошибка", repr(e))
        try:
            self._tick_heartbeat(now)
        except Exception as e:                                   # noqa: BLE001
            log("tick heartbeat ошибка", repr(e))
        try:
            self._tick_scheduled_jobs(now)
        except Exception as e:                                   # noqa: BLE001
            log("tick jobs ошибка", repr(e))
        self.save()

    def _tick_alerts(self):
        """Ревью 04.07: падение боевого cron-прогона больше не тихое — ops/cron_alert.sh пишет
        journal/alerts.jsonl, мы пушим новые строки (курсор в state, доставка с ретраем)."""
        import json as _json
        path = ROOT / "journal" / "alerts.jsonl"
        if not path.exists():
            return
        seen = int(self.state.get("alerts_seen", 0))
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines[seen:], start=seen):
            try:
                rec = _json.loads(line)
            except ValueError:
                self.state["alerts_seen"] = i + 1     # битую строку не перечитываем вечно
                continue
            pull_day = None
            if rec.get("label") == "git_pull":
                # ежечасная протяжка: повторный сбой в те же сутки — одним алертом, не 24-мя
                day = str(rec.get("ts") or "")[:10] + ":" + str(rec.get("exit"))
                days = self.state.setdefault("git_pull_alert_days", {})   # перепроверка №6: по коду
                legacy = self.state.pop("git_pull_alert_day", None)      # старая отметка «день:код» → в словарь
                if isinstance(legacy, str) and ":" in legacy:
                    days.setdefault(legacy.rsplit(":", 1)[1], legacy)
                if days.get(str(rec.get("exit"))) == day:
                    self.state["alerts_seen"] = i + 1
                    continue
                pull_day = day                          # отметка — только после доставки
                if rec.get("exit") in (10, "10"):
                    try:
                        paths = (ROOT / "journal" / "pull_refused.txt").read_text(encoding="utf-8").split()
                    except OSError:
                        paths = []
                    msg = ("🚨 В GitHub пришёл облачный коммит, который меняет настройки владельца ("
                           + (", ".join(paths[:5]) if paths else "лимиты, веса, рубрику или пороги")
                           + f"; {rec.get('ts')}). Сам сервер такое не применяет — нужно твоё «ок»: посмотри "
                           "коммит и, если согласен, обнови сервер вручную одной командой:\n" + OWNER_PULL_CMD
                           + "\nДо этого правки кода до сервера не доходят; " + self._ideas_flow_note() + ".")
                    if self.tg.send_message(self.chat_id, msg) is None:
                        break
                    self.state.setdefault("git_pull_alert_days", {})[str(rec.get("exit"))] = pull_day
                    self.state["alerts_seen"] = i + 1
                    continue
                why, fix = PULL_CODES.get(str(rec.get("exit")), ("причина в journal/cron.log", "пришли мне хвост journal/cron.log"))
                msg = (f"🚨 Сервер не смог забрать правки кода с GitHub (код {rec.get('exit')}, "
                       f"{rec.get('ts')}): {why}. {self._ideas_flow_note().capitalize()}. Что сделать: {fix}.")
            elif rec.get("label") == "checkpoint":
                why = CHECKPOINT_CODES.get(str(rec.get("exit")), "причина в journal/cron.log")
                msg = (f"🚨 Вечерний чекпоинт (журналы сервера → GitHub) не прошёл (код {rec.get('exit')}, "
                       f"{rec.get('ts')}): {why}.")
            else:
                msg = (f"🚨 Задача сервера «{rec.get('label')}» упала (код {rec.get('exit')}, "
                       f"{rec.get('ts')}). Хвост ошибки — в journal/cron.log; если повторится завтра — "
                       "пришли мне его.")
            if self.tg.send_message(self.chat_id, msg) is None:
                break                                  # не дошло — ретрай со следующего tick
            if pull_day:
                self.state.setdefault("git_pull_alert_days", {})[str(rec.get("exit"))] = pull_day
            self.state["alerts_seen"] = i + 1
            log("пуш алерта cron", rec.get("label"), rec.get("exit"))

    def _tick_notices(self):
        """Автопетля §25 (решение владельца 09.07): ops/auto_review.py пишет заметки
        (разборы/инсайты/доступные корректировки) в journal/notices.jsonl — пушим новые
        по курсору, как алерты (доставка с ретраем; текст готов на стороне автора заметки)."""
        import json as _json
        path = ROOT / "journal" / "notices.jsonl"
        if not path.exists():
            return
        seen = int(self.state.get("notices_seen", 0))
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines[seen:], start=seen):
            try:
                rec = _json.loads(line)
            except ValueError:
                self.state["notices_seen"] = i + 1     # битую строку не перечитываем вечно
                continue
            text = str(rec.get("text") or "").strip()
            if rec.get("silent"):                      # служебная заметка — не владельцу (P1 №13)
                text = ""
            _mid = self._send_card(text, None) if text else None
            if text and _mid is None:                  # None = часть не дошла (ретрай)
                break                                  # не дошло — ретрай со следующего tick
            if text:
                _src = str(rec.get("source") or "дозор")
                self._log_delivery({"deep_dive": "день прокачки", "auto_review": "пятничный разбор"}.get(_src, "заметка: " + _src),
                                   ref=f"notices#{i}", text=text, mid=_mid)
            self.state["notices_seen"] = i + 1
            log("пуш заметки §25", (text[:60] or "<пусто>"))

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
                    # Этап3: «Разбор дня» — читаемый кейс без эмодзи, проверенный линтером стиль-
                    # контракта (fail-closed). При daily_case_lead=true он ГЛАВНЫЙ дневной пуш вместо
                    # длинного эмодзи-дайджеста (потолок шума: один читаемый артефакт в день).
                    card, _artifact = None, "дайджест"
                    # Ревью #5/#8: сбрасываем «показанную подборку Топ-3» ДО выбора артефакта. Иначе
                    # при фолбэке на «Разбор дня»/дайджест старая last_top3 доживает, и ответ «1/2/3»
                    # раскрыл бы идею ЧУЖОГО (прошлого) прогона как сегодняшнюю. Ставим заново только
                    # если сегодня реально ушёл «Топ-3».
                    self.state["last_top3"] = None
                    # Flow-fix В1 (15.07): лид-каскад ПОДАЧИ — «Топ-3 одной строкой» + защита №1
                    # (выбор владельца) → фолбэк «Разбор дня» → фолбэк эмодзи-дайджест. Каждый слой
                    # fail-closed по линтеру стиль-контракта: сломанный шаблон не уходит, берём следующий.
                    # V0 «Идея дня» (карта v3, 31.10; формат владельца 15.07): ГЛАВНЫЙ пуш. Линт
                    # блокирующий для ЭТОГО формата: не прошёл → фолбэк на Топ-3 (тоже через линт)
                    # + заметка в дозор (stage-review V0 №3: иначе один чужой глиф снимал бы утро).
                    # Остальные идеи — по запросу «что ещё было» (→ Топ-3 того же прогона).
                    lint_blocked = False
                    obs_suppressed = False
                    # идея охотника (кнопки) не стирается серверным прогоном — её сменит только
                    # следующая защищаемая идея
                    if (self.state.get("last_idea_day") or {}).get("source") != "hunter":
                        self.state["last_idea_day"] = None
                    if pres.get("idea_day_lead"):
                        id_text, id_meta, id_viol = R.idea_of_day_from_protocol(proto)
                        # Архитектура 02.11 (вердикт владельца «даёшь идею — и тут же убиваешь»):
                        # жанр «наблюдение» (защищать нечего) главным пушем НЕ уходит — идея дня
                        # приходит от охотника; конвейер доступен по «что ещё было».
                        if (id_text is not None and not pres.get("server_observation_push", True)
                                and (id_meta or {}).get("genre") == "наблюдение"):
                            self.state["last_top3"] = {"run_id": run_id,
                                                       "idx": (id_meta or {}).get("idx") or {}}
                            obs_suppressed = True
                            log("Идея дня в жанре «наблюдение» — главным пушем не шлю (идея дня "
                                "— от охотника)", run_id)
                        elif id_text is None:
                            # stage-review V0 (регрессия №3): без фолбэка один чужой глиф снимал бы
                            # утренний пуш целиком. Фолбэк — Топ-3 (тоже через линт) + заметка в дозор.
                            log("Идея дня НЕ прошла линт подачи — фолбэк на Топ-3 (V0)", run_id, id_viol)
                            self._notice_dozor(
                                "«Идея дня» сегодня не прошла проверку подачи "
                                "(" + "; ".join(str(v) for v in (id_viol or [])[:3]) + ") — "
                                "утром ушёл список идей вместо одной защищаемой. Это защита от "
                                "сломанного текста, не отсутствие идей.")
                        else:
                            card = id_text
                            _artifact = "Идея дня"
                            # 02.11 (вердикт владельца «ничего не понимаю»): редактор-рассказчик.
                            # За флагом (правило внедрения); отказ/нарушение гардов → строгий
                            # формат уходит как есть + заметка в дозор (пуш не теряется).
                            if pres.get("story_editor"):
                                try:
                                    import story_editor as SE
                                    # Ре-гейт V0 02.11: кэш по run_id — сетевой ретрай отправки
                                    # НЕ прожигает второй LLM-вызов (и отказ не переспрашивается)
                                    _sc = self.state.get("story_cache") or {}
                                    if _sc.get("run_id") == run_id:
                                        story, why = _sc.get("text"), _sc.get("fail")
                                    else:
                                        story, why = SE.rewrite(id_text, run_id=run_id)
                                        self.state["story_cache"] = {"run_id": run_id,
                                                                     "text": story, "fail": why}
                                        self.save()
                                    if story:
                                        card = story
                                        _artifact = "Идея дня (рассказ)"
                                    elif _sc.get("run_id") != run_id:
                                        self._notice_dozor(
                                            "Рассказчик подачи сегодня не прошёл проверку "
                                            f"({why}) — ушёл строгий формат. Это защита от "
                                            "выдуманных чисел, не поломка.")
                                except Exception as e:       # noqa: BLE001
                                    log("story_editor ошибка", repr(e))
                            self.state["last_idea_day"] = {"run_id": run_id,
                                                           "idx": (id_meta or {}).get("idx") or {},
                                                           "lead": (id_meta or {}).get("lead"),
                                                           "issued_at": issued_at}
                            # ответ «1» раскрывает защиту лида из готового суда (как у Топ-3)
                            self.state["last_top3"] = {"run_id": run_id,
                                                       "idx": (id_meta or {}).get("idx") or {}}
                            self.state["last_case"] = None
                    if lint_blocked:
                        self.state["last_idea_day"] = {"run_id": run_id, "idx": {}}
                    if card is None and not (lint_blocked or obs_suppressed) and pres.get("top3_lead"):
                        t3_text, t3_meta, t3_viol = R.top3_from_protocol(proto)
                        if t3_text is None:
                            log("Топ-3 не прошёл стиль-контракт — фолбэк на Разбор дня", run_id, t3_viol)
                        else:
                            card = t3_text
                            _artifact = "Топ-3"
                            # запоминаем показанные идеи: ответ владельца «1»/«2»/«3» раскроет защиту
                            # соответствующей идеи из ГОТОВОГО суда (без нового прогона).
                            self.state["last_top3"] = {"run_id": run_id,
                                                       "idx": (t3_meta or {}).get("idx") or {}}
                            self.state["last_case"] = None
                    if card is None and not (lint_blocked or obs_suppressed) and pres.get("daily_case_lead"):
                        case_text, _case, _viol = R.daily_case_from_protocol(proto)
                        if case_text is None:                # линтер завалил шаблон → не шлём сломанное
                            log("Разбор дня не прошёл стиль-контракт — фолбэк на дайджест", run_id, _viol)
                        else:
                            card = case_text
                            _artifact = "Разбор дня"
                            # Этап3: запоминаем показанный кейс — свободный ответ владельца на вопрос
                            # («Твой ход: …») попадёт в decisions_user.jsonl как разметка качества (§25).
                            q = (_case or {}).get("вопрос") or {}
                            self.state["last_case"] = {
                                "run_id": run_id, "asset": (_case or {}).get("актив"),
                                "status": (_case or {}).get("статус"),
                                "варианты": q.get("варианты") or []}
                    if card is None and not (lint_blocked or obs_suppressed):    # флаги выключены ИЛИ линтер завалил → дайджест
                        card = R.format_research_digest(proto)
                    if lint_blocked or obs_suppressed:
                        self.state["digest_sent"].append(run_id)   # V0: пуша нет (заметка/охотник); не ретраим
                    else:
                        # длиннее лимита Telegram (4096) → шлём частями.
                        # V2: у «Идеи дня» — кнопки действия (клавиатура на последней части);
                        # впервые у ежедневного пуша есть отклик в один клик (§12-петля).
                        kb_lead = None
                        # Ре-гейт №2 02.11 (регрессия high): рассказ («Идея дня (рассказ)») — тот же
                        # дневной лид и ОБЯЗАН нести кнопки, иначе включение story_editor молча убьёт
                        # петлю §12/deep_dive (ровно «мёртвую петлю владельца», которую чинил V2).
                        if _artifact.startswith("Идея дня") and (self.state.get("last_idea_day") or {}).get("lead"):
                            kb_lead = self._idea_kb(run_id)
                        if self._send_card(card, kb_lead) is None:
                            log("сбой отправки дневного артефакта — ретрай в следующий tick", run_id)
                            continue
                        self.state["digest_sent"].append(run_id)
                        if _artifact.startswith("Идея дня"):     # защищаемая идея закрывает день
                            self.state["idea_day_delivered"] = S.now_utc().date().isoformat()
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
                log(f"пуш дневного артефакта event-first ({_artifact})", run_id)
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
        # аудит 03.11 (P1 №13, шум): строка «сколько стоит система» — раз в неделю, по пятницам;
        # алерт о потолках ниже — по-прежнему в день срабатывания
        if now.hour >= BUDGET_HOUR and now.isoweekday() == 5 and self.state.get("last_budget_line_date") != today:
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
        quiet = bool(self.state.pop("quiet_restart", False))
        if quiet:
            self.save()
        # 03.11 (отзыв владельца: два «на связи» подряд — шум): приветствие только при самом первом
        # запуске; перезапуски после обновлений — молча
        if self.chat_id and not quiet and not self.state.get("greeted"):
            if self.tg.send_message(self.chat_id, "🤖 «Оракул» на связи. Буду присылать идеи простым "
                                                  "языком и отвечать на вопросы. Нажми /help, чтобы понять, "
                                                  "что я умею.") is not None:
                self.state["greeted"] = True
                self.save()
        code_sig, next_check = _code_signature(), time.time() + 60
        while True:
            try:
                ups = self.tg.get_updates(self.state.get("update_offset", 0))
                for upd in ups:
                    self.handle_update(upd)
                if ups:
                    self.save()
                self.tick()
                if time.time() >= next_check:
                    next_check = time.time() + 60
                    if self._maybe_restart_on_new_code(code_sig):
                        return
            except Exception as e:                               # noqa: BLE001
                log("петля ошибка", repr(e))
                time.sleep(3)

    def _maybe_restart_on_new_code(self, code_sig):
        """Ревью 02.11: ежечасный git pull приносит новый код, а процесс бота живёт на старом —
        он доставил бы идею охотника без печати. Код изменился и компилируется → тихий выход
        (systemd Restart=always поднимет через 5 с без приветствия); не компилируется → не
        перезапускаемся, заметка в дозор (иначе цикл падений)."""
        sig = _code_signature()
        if sig == code_sig:
            return False
        if self._job_busy():                         # не рвём «Качаем», /run-funnel, /debate на лету
            return False
        # ре-гейт 03.11: задачи за флагами (прокачка, «Шансы недели», снимок цен) — отдельные процессы в той
        # же группе systemd: выход бота убил бы их на середине. Перезапуск — после их завершения
        if any(pr.poll() is None for pr in (getattr(self, "_job_procs", None) or {}).values()):
            return False
        bad = []
        for f in _code_files():
            try:                                     # синтаксис без записи .pyc (ничего не пишем)
                compile(f.read_text(encoding="utf-8"), str(f), "exec")
            except (SyntaxError, ValueError, OSError) as e:
                bad.append(f"{f.name}: {str(e)[:80]}")
        if not bad:
            # compile() не ловит ошибки импорта — пробный запуск самопроверки в отдельном процессе
            import subprocess
            try:
                here = pathlib.Path(__file__).resolve()
                r = subprocess.run([sys.executable, str(here), "--check"],
                                   capture_output=True, text=True, timeout=60, cwd=str(here.parents[1]))
                if r.returncode not in (0, 1):
                    bad.append("самопроверка: " + (r.stderr or r.stdout)[-120:])
                elif "Traceback" in (r.stderr or ""):
                    bad.append("самопроверка: " + r.stderr[-120:])
            except Exception as e:                   # noqa: BLE001
                bad.append(f"самопроверка не запустилась: {type(e).__name__}")
        if bad:
            if self.state.get("bad_code_sig") != sig:
                self.state["bad_code_sig"] = sig
                self._notice_owner("Сервер забрал новый код, но он не компилируется (" + "; ".join(bad[:2])
                                   + ") — бот остаётся на прежнем коде до починки.")
                self.save()
            return False
        log("код обновился — тихий перезапуск ради новой версии")
        self.state["quiet_restart"] = True
        self.save()
        return True


def _code_files():
    out = []
    for sub in ("ops", "orchestrator", "mathlib", "data", "agents"):
        d = ROOT / sub
        if d.exists():
            out += [f for f in d.rglob("*.py") if "tests" not in f.parts and "__pycache__" not in f.parts]
    return out


def _code_signature():
    """Подпись кода бота: (число .py, сумма mtime) — дёшево и меняется при любом pull кода."""
    files = _code_files()
    try:
        return (len(files), round(sum(f.stat().st_mtime for f in files), 3))
    except OSError:
        return None


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
    # перепроверка №2: тихий перезапуск проверяет новый код этой самопроверкой — модули охоты
    # импортируются тут явно, иначе сломанный импорт hunter_* молча выключил бы доставку идей
    try:
        try:
            from ops import hunter_seal as _HS, hunter_court as _HC, idea_gate as _G   # noqa: F401
        except ImportError:
            import hunter_seal as _HS, hunter_court as _HC, idea_gate as _G   # noqa: F401
        from mathlib import base_odds as _BO                                    # noqa: F401
        print("модули охоты: OK")
    except Exception as e:                                       # noqa: BLE001
        print("модули охоты: ОШИБКА", repr(e))
        return 2
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
    return 0                       # выход ради нового кода — systemd (Restart=always) поднимет


if __name__ == "__main__":
    sys.exit(main())
