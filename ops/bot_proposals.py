# -*- coding: utf-8 -*-
"""ops/bot_proposals.py — V2 (карта v3): предложения петли §25 в человеческий вид с кнопками.

Мёртвая петля №4 аудита 31.10: пятничный авторазбор (ops/auto_review.py) складывал
devops-предложения в journal/proposals/proposals_*.json — и они лежали неподписанными
неделями (18.10, 25.10): канал требовал, чтобы владелец читал машинный JSON. Теперь:
раз в неделю (воскресенье — суббота занята темами) бот шлёт карточку с предложениями
простыми словами и кнопками ✅ подписать / ❌ отклонить; решения — append-only в
journal/proposal_decisions.jsonl. ПОДПИСАННОЕ ПРИМЕНЯЕТ МАШИНИСТ через git с ревью
(§10: N≥30, шаг ≤±10%; инвариант 5 — пороги/лимиты только с подписью) — бот конфиги
и веса НЕ трогает.
"""
import json
import hashlib
import pathlib
import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROP_DIR = ROOT / "journal" / "proposals"
DECISIONS = ROOT / "journal" / "proposal_decisions.jsonl"

BATCH = 5
_TXT_MAX = 180


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _load_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def proposal_key(file_ts, item):
    """Устойчивый ключ предложения: ts файла + суть. Повтор того же предложения
    в следующем файле — НОВЫЙ ключ (данные другой недели), это сознательно."""
    raw = f"{file_ts}|{str(item.get('предложение') or '')[:100]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def latest_file(prop_dir=None):
    d = pathlib.Path(prop_dir or PROP_DIR)
    if not d.exists():
        return None
    files = sorted(d.glob("proposals_*.json"))
    return files[-1] if files else None


def pending(prop_dir=None, decisions_path=None, limit=BATCH):
    """Неразобранные предложения СВЕЖЕЙШЕГО файла петли: [(ключ, {'ts','item'}), …]."""
    f = latest_file(prop_dir)
    if f is None:
        return []
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return []
    ts = str(doc.get("ts") or f.stem)
    decided = {d.get("ключ") for d in _load_jsonl(decisions_path or DECISIONS)}
    out = []
    for item in (doc.get("предложения") or []):
        if not isinstance(item, dict):
            continue
        k = proposal_key(ts, item)
        if k in decided:
            continue
        out.append((k, {"ts": ts, "файл": f.name, "item": item}))
    return out[:limit]


def _cut(s):
    s = str(s or "").strip()
    return s if len(s) <= _TXT_MAX else s[:_TXT_MAX - 1].rstrip() + "…"


def format_card(items):
    """Карточка простыми словами. items — из pending(). Пусто → None (карточка не шлётся)."""
    if not items:
        return None
    L = ["Петля качества раз в неделю смотрит на машину и предлагает поправки. Решение",
         "всегда твоё: ✅ — подписываю (внесёт машинист через git, с проверкой и тестами,",
         "не в ту же секунду), ❌ — отклонить. Ничего само не меняется.",
         ""]
    for i, (_k, row) in enumerate(items, 1):
        it = row["item"]
        L.append(f"{i}. Что замечено: {_cut(it.get('наблюдение'))}")
        L.append(f"   Что предлагается: {_cut(it.get('предложение'))}")
        L.append(f"   Что это даст: {_cut(it.get('эффект'))}")
    L.append("")
    L.append("Кнопки ниже: ✅ подписать / ❌ отклонить — по номеру.")
    return "\n".join(L)


def build_keyboard(items):
    """Инлайн-клавиатура: ряд на предложение — [✅ N] [❌ N]; callback 'p:y|n:<ключ>'."""
    rows = []
    for i, (k, _r) in enumerate(items, 1):
        rows.append([{"text": f"✅ {i}", "callback_data": f"p:y:{k}"},
                     {"text": f"❌ {i}", "callback_data": f"p:n:{k}"}])
    return {"inline_keyboard": rows}


def record_decision(key, decision, row, chat_id, decisions_path=None, now=None):
    """Решение владельца → journal/proposal_decisions.jsonl (append-only). decision:
    'signed' | 'rejected'. Подпись здесь = авторизация §10 на внесение машинистом."""
    it = row.get("item") or {}
    rec = {
        "ts": (now or _now()).isoformat(timespec="seconds"),
        "type": "proposal_decision",
        "ключ": key,
        "решение": decision,
        "наблюдение": it.get("наблюдение"),
        "предложение": it.get("предложение"),
        "эффект": it.get("эффект"),
        "требует_подписи": it.get("требует_подписи"),
        "файл_источник": row.get("файл"),
        "ts_файла": row.get("ts"),
        "source": "telegram_bot",
        "chat_id": chat_id,
    }
    path = pathlib.Path(decisions_path or DECISIONS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
