# -*- coding: utf-8 -*-
"""ops/mechanism_match.py — V6, приоритет испытателя №1: идея дня матчится на механизм
библиотеки knowledge/mechanisms.yaml, и владельцу печатается «похожий случай» —
датированный прецедент С КОНТРПРИМЕРОМ (аргументация библиотеки дотекает до пуша).

Детерминированно (Инв#6): пересечение словарей (токены ≥4 букв, минус стоп-слова)
текста идеи (повод + звенья цепочки) с словарём механизма (rule + trigger_class +
watch_assets + chain). Слабое пересечение → честный None, строка не печатается (П8:
не навешиваем чужой прецедент на непохожую идею).

Числа прецедента всегда внутри кавычек «…» — линт ≤4 чисел вне цитат не ломается.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MECHS = ROOT / "knowledge" / "mechanisms.yaml"

MIN_OVERLAP = 4          # минимум общих содержательных токенов — ниже матч не объявляем

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]{3,}")
_SHORT_OK = {"газ", "спг", "сша", "нпз", "цод", "ткс", "опек"}   # значимые короткие
_TAIL = "аяоеиыуюьй"                                              # окончания к срезу
_STOP = {
    "этот", "этого", "который", "которые", "после", "перед", "через", "между", "против",
    "рынок", "рынка", "рынке", "цена", "цены", "ценой", "рост", "роста", "компания",
    "компании", "акции", "акций", "недель", "неделю", "месяц", "месяца", "день", "дней",
    "выше", "ниже", "больше", "меньше", "сильнее", "слабее", "первый", "второй", "третий",
    "также", "если", "когда", "чтобы", "может", "могут", "будет", "была", "были", "есть",
    "this", "that", "with", "from", "into", "over", "under", "after", "before", "price",
    "market", "stock", "stocks", "week", "weeks",
}


def _norm(t):
    """Грубый стеммер: срезать до двух хвостовых гласных/«й», затем префикс 6 —
    «газа»→«газ», «поставок»/«поставки»→«постав», «танкерный»→«танкер» (Инв#6, без LLM)."""
    for _ in range(2):
        if len(t) > 3 and t[-1] in _TAIL:
            t = t[:-1]
    return t[:6]


_STOP_N = None


def _tokens(text):
    global _STOP_N
    if _STOP_N is None:
        _STOP_N = {_norm(s) for s in _STOP}
    out = set()
    for t in _TOKEN_RE.findall(str(text or "").lower()):
        if len(t) == 3 and t not in _SHORT_OK:
            continue
        n = _norm(t)
        if t in _STOP or n in _STOP_N:
            continue
        out.add(n)
    return out


def load_index(path=None):
    """[{id, название, bag, precedents, counterexamples}] — только торгуемые с прецедентами."""
    p = pathlib.Path(path or MECHS)
    if not p.exists():
        return []
    rows = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("механизмы") or []
    out = []
    for m in rows:
        if not m.get("tradeable") or not (m.get("precedents") or []):
            continue
        bag = _tokens(" ".join([str(m.get("rule") or ""), str(m.get("trigger_class") or ""),
                                str(m.get("watch_assets") or ""),
                                " ".join(str(c) for c in (m.get("chain") or []))]))
        out.append({"id": m.get("id"), "название": m.get("название") or m.get("id"),
                    "bag": bag, "precedents": m.get("precedents") or [],
                    "counterexamples": str(m.get("counterexamples") or "")})
    return out


def match(idea_text, index=None, min_overlap=MIN_OVERLAP):
    """Текст идеи → (механизм, счёт-пересечение) либо None (честно: похожего не нашли)."""
    toks = _tokens(idea_text)
    if not toks:
        return None
    best, best_score = None, 0
    for m in (index if index is not None else load_index()):
        score = len(toks & m["bag"])
        if score > best_score or (score == best_score and best and m["id"] < best["id"]):
            best, best_score = m, score
    if best is None or best_score < min_overlap:
        return None
    return best, best_score


def _trunc_phrase(text, limit):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in (". ", "; ", " — ", ", "):
        i = cut.rfind(sep)
        if i >= limit // 2:
            return cut[:i].rstrip(".;, ")
    return cut.rsplit(" ", 1)[0].rstrip(".;, ")


def similar_case_line(mech):
    """Механизм → строка «Похожий случай…» для пуша: прецедент С ЧИСЛОМ + контрпример.
    Все числа — внутри «…» (цитаты, линт их не считает). Нет пригодного прецедента → None."""
    prec = next((p for p in mech["precedents"]
                 if re.search(r"\d", str(p.get("observed") or ""))), None)
    if not prec:
        return None
    случай = _trunc_phrase(f"{prec.get('year')}: {prec.get('event')} — {prec.get('observed')}", 180)
    line = (f"Похожий случай (механизм «{mech['название']}»). «{случай}»")
    counter = _trunc_phrase(mech.get("counterexamples"), 150)
    if counter:
        line += f" Но бывало иначе: «{counter}»."
    else:
        line += "."
    return line
