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


def _instruments(row):
    return tuple(sorted((row.get("целевой_дальний_узел") or {}).get("instruments") or []))


def theme_key(row):
    """Устойчивый ключ темы — НАБОР БУМАГ целевого узла (аудит 03.11: в ключ входил текст описания,
    и 1660 формулировок одной и той же темы считались 1660 темами — танкеры всплывали каждую неделю).
    Без бумаг — по усечённому описанию."""
    instruments = _instruments(row)
    if instruments:
        raw = "|".join(instruments)
    else:
        raw = "~" + ((row.get("целевой_дальний_узел") or {}).get("node") or row.get("событие") or "")[:80]
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def pending_queue(proposed_path=None, decisions_path=None, limit=THEME_BATCH):
    """Лучшие неразобранные темы. Группа = набор бумаг; похожие наборы (общих бумаг ≥ половины)
    склеиваются и с уже разобранными, и между собой; в группе — самая сильная и свежая
    формулировка; «сколько раз предлагалась» и «сколько недель» — в строке темы."""
    decisions = _load_jsonl(decisions_path or DECISIONS)
    decided_keys = {d.get("ключ") for d in decisions}
    decided_sets = [tuple(d.get("инструменты") or []) for d in decisions if d.get("инструменты")]
    groups = {}
    for r in _load_jsonl(proposed_path or PROPOSED):
        k = theme_key(r)
        g = groups.setdefault(k, {"best": r, "n": 0, "weeks": set()})
        g["n"] += 1
        try:
            g["weeks"].add(datetime.date.fromisoformat(str(r.get("ts"))[:10]).strftime("%G-%V"))
        except ValueError:
            pass
        cur = g["best"]
        if ((r.get("тектонический_потенциал") or 0.0), r.get("ts") or "") > \
                ((cur.get("тектонический_потенциал") or 0.0), cur.get("ts") or ""):
            g["best"] = r
    ranked = sorted(groups.items(), key=lambda kv: (-(kv[1]["best"].get("тектонический_потенциал") or 0.0),
                                                    -kv[1]["n"], kv[0]))
    out, picked = [], []
    for k, g in ranked:
        inst = _instruments(g["best"])
        if k in decided_keys or any(_jaccard(inst, d) >= 0.5 for d in decided_sets):
            continue
        if any(_jaccard(inst, p) >= 0.5 for p in picked):
            continue
        row = dict(g["best"], _раз=g["n"], _недель=len(g["weeks"]))
        out.append((k, row))
        picked.append(inst)
        if len(out) >= limit:
            break
    return out


# термин оставляем в своей форме (падежи!) и расшифровываем при первом упоминании — правило подачи
_EXPLAIN = ((r"([Чч]окпоинт\w*)", "узкое место"), (r"([Гг]иперскейлер\w*)", "крупный облачный провайдер"),
            (r"([Кк]апекс\w*|capex)", "инвестиции в мощности"), (r"(ЦОД\w*)", "дата-центр"),
            (r"(\b[Uu]tilities\b)", "энергокомпании"), (r"(\b[Ии]нференс\w*)", "работа обученной нейросети"),
            (r"(\b[Кк]оммодитизаци\w*)", "превращение в дешёвый массовый товар"))


def _explain(text):
    import re
    for pat, plain in _EXPLAIN:
        text = re.sub(pat, lambda m, pl=plain: f"{m.group(1)} ({pl})", text, count=1)
    return text


def _times(n):
    n = int(n)
    return f"{n} раза" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else f"{n} раз"


def _named(ticker):
    """«Frontline (FRO)»; имени в словаре нет — честно «WPM (название уточню при разборе)» (П8)."""
    base = str(ticker).split(".")[0]
    try:
        import bot_reports as R
        name = R._asset_human(ticker)[0]
    except Exception:                              # noqa: BLE001
        name = ticker
    return f"{name} ({base})" if name and name != ticker else f"{base} (название уточню при разборе)"


def _short(row):
    tgt = row.get("целевой_дальний_узел") or {}
    node = (tgt.get("node") or row.get("событие") or "тема без описания").strip()
    if len(node) > _TITLE_MAX:
        node = node[:_TITLE_MAX - 1].rstrip() + "…"
    node = _explain(node)
    instr = ", ".join(_named(t) for t in (tgt.get("instruments") or [])[:4]) or "бумаги не названы"
    return node, instr


def format_card(items):
    """Текст еженедельной карточки. items — список (ключ, строка-тема) из pending_queue."""
    if not items:
        return None
    L = ["Темы недели: система замечала их в новостях снова и снова. Какие тебе интересны?",
         "«Да» — охотник будет искать идею дня в этой теме в первую очередь. «Нет» — больше",
         "не предложу её (и похожие на неё).",
         ""]
    for i, (_k, r) in enumerate(items, 1):
        node, instr = _short(r)
        L.append(f"{i}. {node}")
        times = r.get("_раз")
        L.append(f"   Бумаги: {instr}." + (f" Всплывала {_times(times)} за {r.get('_недель') or 1} нед." if times and times > 1 else ""))
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
