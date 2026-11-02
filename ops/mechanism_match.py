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


# Ре-гейт №2 02.11 (high): прежний разбор знал ТОЛЬКО русские глаголы подстрокой — 95 из 126
# числовых прецедентов библиотеки («+164%», «−~40%», «$3 → $12») давали «знак неизвестен», и
# противонаправленный прецедент печатался доводом «за». Теперь: (1) словесные маркеры матчатся
# ПРЕФИКСОМ токена («рост» не ловится внутри «просто»/«прирост»); (2) «кратно» убран —
# двунаправленный усилитель («упал кратно»); (3) знак читается и из ЧИСЛОВОЙ нотации:
# «+X%»/«−X%» и сравнение «X → Y».
_OBS_UP = ("рост", "вырос", "подорожа", "взлет", "взлёт", "прибав", "удвои", "утрои",
           "рекорд", "подскоч", "вверх", "плюс", "удорожа")
_OBS_DOWN = ("обвал", "рухнул", "падени", "снизил", "сжал", "дешев", "упал", "упад",
             "просел", "сполз", "вниз", "минус")
_PLAIN_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _obs_sign(observed):
    t = str(observed or "").lower()
    up = "↑" in t
    down = "↓" in t
    toks = re.findall(r"[а-яё]+", t)
    up = up or any(tok.startswith(w) for tok in toks for w in _OBS_UP)
    down = down or any(tok.startswith(w) for tok in toks for w in _OBS_DOWN)
    # знак при числе: «+164%» → вверх, «−~40%» → вниз; дефис-диапазон «3-5» не знак
    for m in re.finditer(r"[+\-−]\s*[~≈]?\s*\d", t):
        prev = t[m.start() - 1] if m.start() > 0 else " "
        if m.group(0)[0] in "-−" and prev.isalnum():
            continue
        if m.group(0)[0] == "+":
            up = True
        else:
            down = True
    # сравнение «X → Y» («$3 → ~$12/б»): последняя цифра слева против первой справа
    parts = re.split(r"→|->", t)
    for a, b in zip(parts, parts[1:]):
        ma, mb = _PLAIN_NUM_RE.findall(a), _PLAIN_NUM_RE.findall(b)
        if ma and mb:
            x, y = float(ma[-1].replace(",", ".")), float(mb[0].replace(",", "."))
            if y > x:
                up = True
            elif y < x:
                down = True
    if up and not down:
        return "вверх"
    if down and not up:
        return "вниз"
    return None                                   # смешанный/неопределённый — знак не утверждаем


def similar_case_line(mech, direction=None):
    """Механизм → строка «Похожий случай…» для пуша: прецедент С ЧИСЛОМ + контрпример.
    Все числа — внутри «…» (цитаты, линт их не считает). Нет пригодного прецедента → None.

    Ре-гейт №2 02.11 (знак-гард, ужесточение): при известном направлении идеи печатается
    ТОЛЬКО прецедент, чей знак ПОДТВЕРЖДЁН и совпадает. Прежний фолбэк «знак неизвестен —
    сойдёт» подкладывал под идею «вниз» явно восходящие прецеденты, которые слабый
    классификатор не прочёл (1973: нефть $3→$12 под идеей «вниз»). Неподтверждённый знак =
    строки честно нет (П8: отсутствие довода лучше довода-переворота)."""
    numeric = [p for p in mech["precedents"]
               if re.search(r"\d", str(p.get("observed") or ""))]
    if not numeric:
        return None
    if direction in ("вверх", "вниз"):
        prec = next((p for p in numeric if _obs_sign(p.get("observed")) == direction), None)
        if prec is None:
            return None
    else:
        prec = numeric[0]
    случай = _trunc_phrase(f"{prec.get('year')}: {prec.get('event')} — {prec.get('observed')}", 180)
    line = (f"Похожий случай (механизм «{mech['название']}»). «{случай}»")
    counter = _trunc_phrase(mech.get("counterexamples"), 150)
    if counter:
        line += f" Но бывало иначе: «{counter}»."
    else:
        line += "."
    return line
