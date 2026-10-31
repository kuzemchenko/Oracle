# -*- coding: utf-8 -*-
"""ops/bot_themes.py — V2 (карта v3): еженедельная карточка «утверди темы» в Telegram.

Мёртвая петля №3 аудита 31.10: картограф предложил 1600+ тем-кластеров
(journal/proposed_themes.jsonl, «предложено… НЕ торгуется до утверждения §30/П16»),
утверждено за всю жизнь — ноль: канал требовал ручной правки конфига, которой
никогда не случалось. Теперь: раз в неделю бот шлёт пакет из THEME_BATCH лучших
неразобранных тем с кнопками ✅/❌; решения пишутся append-only в
journal/theme_decisions.jsonl. КОНФИГ (config/universe.yaml) бот НЕ трогает —
утверждённые темы вносит еженедельная сессия-машинист через git (провенанс,
ревью, тесты), как требует §30/П16.

Подпись владельца 31.10 (journal/proposals/signature_20261031_roadmap_v3.md, П5):
пакетный разбор очереди тем кнопками.
"""
import json
import hashlib
import pathlib
import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROPOSED = ROOT / "journal" / "proposed_themes.jsonl"
DECISIONS = ROOT / "journal" / "theme_decisions.jsonl"

THEME_BATCH = 5
_TITLE_MAX = 160


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _load_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def theme_key(row):
    """Устойчивый ключ темы: инструменты целевого узла + усечённое описание.
    Один и тот же кластер, предложенный много раз, разбирается один раз."""
    tgt = row.get("целевой_дальний_узел") or {}
    instruments = tuple(sorted(tgt.get("instruments") or []))
    node = (tgt.get("node") or row.get("событие") or "")[:80]
    raw = "|".join(instruments) + "|" + node
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def pending_queue(proposed_path=None, decisions_path=None, limit=THEME_BATCH):
    """Лучшие неразобранные темы: дедуп по ключу, сортировка по тектоническому
    потенциалу (сильные вперёд), при равном — свежие вперёд."""
    decided = {d.get("ключ") for d in _load_jsonl(decisions_path or DECISIONS)}
    best = {}
    for r in _load_jsonl(proposed_path or PROPOSED):
        k = theme_key(r)
        if k in decided:
            continue
        cur = best.get(k)
        if cur is None or (r.get("ts") or "") > (cur.get("ts") or ""):
            best[k] = r
    rows = sorted(best.items(), key=lambda kv: kv[1].get("ts") or "", reverse=True)
    rows = sorted(rows, key=lambda kv: -(kv[1].get("тектонический_потенциал") or 0.0))
    return rows[:limit]


def _short(row):
    tgt = row.get("целевой_дальний_узел") or {}
    node = (tgt.get("node") or row.get("событие") or "тема без описания").strip()
    if len(node) > _TITLE_MAX:
        node = node[:_TITLE_MAX - 1].rstrip() + "…"
    instr = ", ".join((tgt.get("instruments") or [])[:4]) or "инструменты не названы"
    return node, instr


def format_card(items):
    """Текст еженедельной карточки. items — список (ключ, строка-тема) из pending_queue."""
    if not items:
        return None
    L = ["Раз в неделю — пополнение библиотеки связей. Картограф предложил темы; какие",
         "взять под наблюдение? «Да» — тема попадёт в универсум при ближайшем обновлении",
         "(вносится с проверкой, не в ту же секунду). «Нет» — больше не предложу её.",
         ""]
    for i, (_k, r) in enumerate(items, 1):
        node, instr = _short(r)
        L.append(f"{i}. {node}")
        L.append(f"   Бумаги: {instr}.")
    L.append("")
    L.append("Кнопки ниже: ✅ взять / ❌ мимо — по номеру темы.")
    return "\n".join(L)


def build_keyboard(items):
    """Инлайн-клавиатура: ряд на тему — [✅ N] [❌ N]; callback 't:y|n:<ключ>'."""
    rows = []
    for i, (k, _r) in enumerate(items, 1):
        rows.append([{"text": f"✅ {i}", "callback_data": f"t:y:{k}"},
                     {"text": f"❌ {i}", "callback_data": f"t:n:{k}"}])
    return {"inline_keyboard": rows}


def record_decision(key, decision, row, chat_id, decisions_path=None, now=None):
    """Решение владельца по теме → journal/theme_decisions.jsonl (append-only).
    decision: 'approved' | 'rejected'. Возвращает записанную строку."""
    tgt = row.get("целевой_дальний_узел") or {}
    rec = {
        "ts": (now or _now()).isoformat(timespec="seconds"),
        "type": "theme_decision",
        "ключ": key,
        "решение": decision,
        "событие": row.get("событие"),
        "узел": tgt.get("node"),
        "инструменты": tgt.get("instruments") or [],
        "кластер_ключи": row.get("кластер_ключи") or [],
        "тектонический_потенциал": row.get("тектонический_потенциал"),
        "ts_предложения": row.get("ts"),
        "source": "telegram_bot",
        "chat_id": chat_id,
    }
    path = pathlib.Path(decisions_path or DECISIONS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
