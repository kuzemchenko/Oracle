# -*- coding: utf-8 -*-
"""ops/bot_voice.py — голосовые владельца в Telegram → текст (STT) для роутера бота.

Контур: voice (OGG/Opus) → getFile/скачивание → [ffmpeg → mp3, если установлен] →
OpenRouter chat completions с input_audio (роль voice_transcriber, config/models.yaml) →
дословный текст. Дальше текст идёт по ОБЫЧНОМУ пути _on_message — голос эквивалентен
печати: «дай идеи», «разбери GEV», мотив к решению, свободный вопрос.

Инварианты:
  • П8: модель просят вернуть ТОЛЬКО дословный текст; неразборчиво → пустая строка,
    бот честно говорит «не разобрал» (команду за владельца не выдумываем).
  • Бюджет (§30/инвариант 5): каждый вызов — строка в journal/costs.jsonl через
    openrouter.log_cost (agent=voice_transcriber), расход виден в /budget; при
    превышении потолка бот голосовые не распознаёт (гейт в bot._voice_text).
  • Это НЕ суждение о рынке — П10 (состязательность семейств) не затрагивается.
"""
import os
import sys
import base64
import shutil
import pathlib
import subprocess

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator import openrouter as OR   # noqa: E402

ROLE = "voice_transcriber"
MAX_VOICE_SEC = 300          # ~5 минут; длиннее — просим текстом (защита бюджета и лимита Telegram)
HTTP_TIMEOUT = 60
FFMPEG_TIMEOUT = 60

SYSTEM_PROMPT = (
    "Ты — транскрибатор голосовых сообщений. Верни ДОСЛОВНЫЙ текст сообщения на языке "
    "оригинала — без кавычек, комментариев, пояснений и разметки. Если речь неразборчива "
    "или в записи нет слов — верни ровно одно слово: НЕРАЗБОРЧИВО."
)


def ogg_to_mp3(data):
    """OGG/Opus из Telegram → mp3 через ffmpeg (pipe, без временных файлов).
    None — если ffmpeg не установлен или конвертация не удалась (не сбой: тогда
    пробуем отдать модели исходный контейнер как есть)."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0", "-f", "mp3", "-b:a", "64k", "pipe:1"],
            input=data, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    return p.stdout


def transcribe(audio_bytes, fmt, run_id=None):
    """Аудио → текст по цепочке моделей роли voice_transcriber (models.yaml, §26 фолбеки).

    Возвращает дословный текст; '' — модель честно сказала НЕРАЗБОРЧИВО (П8).
    RuntimeError — вся цепочка исчерпана. Каждая попытка логируется в costs.jsonl."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY не задан")
    role_cfg = OR.resolve_role(ROLE)
    chain = [role_cfg["primary"]] + list(role_cfg.get("fallbacks") or [])
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://github.com/oracle-local", "X-Title": "Oracle bot voice"}
    last_err = None
    for model in chain:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "Расшифруй это голосовое сообщение дословно."},
                    {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
                ]},
            ],
            "temperature": role_cfg.get("temperature", 0.0),
            "usage": {"include": True},
        }
        try:
            r = requests.post(OR.OPENROUTER_URL, headers=headers, json=body, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            usage = data.get("usage") or {}
            OR.log_cost("live", ROLE, model, usage, usage.get("cost"),
                        ok=True, run_id=run_id or "bot_voice")
            text = str(text).strip().strip('«»"\'').strip()
            if text.upper().startswith("НЕРАЗБОРЧИВО"):
                return ""                    # честное «не разобрал» (П8), не сбой
            if text:
                return text
            last_err = f"пустой ответ {model}"
        except Exception as e:               # noqa: BLE001 — фолбек на следующую модель (§26)
            OR.log_cost("live", ROLE, model, None, None, ok=False, run_id=run_id or "bot_voice")
            last_err = f"{type(e).__name__}: {e}"
    raise RuntimeError(f"все модели роли {ROLE} исчерпаны: {last_err}")


def voice_to_text(tg, voice, run_id=None):
    """Полный путь: file_id голосового → скачивание из Telegram → STT → текст.

    tg — клиент с get_file(file_id) и download_file(file_path) (ops/bot.Telegram).
    Возвращает текст ('' = неразборчиво); RuntimeError с человеческим объяснением — при сбое."""
    info = tg.get_file(voice.get("file_id"))
    if not info or not info.get("file_path"):
        raise RuntimeError("Telegram не отдал файл голосового (getFile)")
    data = tg.download_file(info["file_path"])
    if not data:
        raise RuntimeError("не удалось скачать файл голосового из Telegram")
    mp3 = ogg_to_mp3(data)
    if mp3 is not None:
        return transcribe(mp3, "mp3", run_id=run_id)
    # ffmpeg нет или конвертация не удалась — пробуем исходный контейнер как есть
    fmt = (voice.get("mime_type") or "audio/ogg").rsplit("/", 1)[-1].lower()
    try:
        return transcribe(data, fmt, run_id=run_id)
    except RuntimeError as e:
        hint = ("; подсказка: поставь ffmpeg на сервере (apt install -y ffmpeg) — "
                "буду конвертировать в mp3 сам" if not shutil.which("ffmpeg") else "")
        raise RuntimeError(f"{e}{hint}") from e
