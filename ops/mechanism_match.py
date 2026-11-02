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
_OBS_DOWN = ("обвал", "рухнул", "пада", "падени", "снизил", "сжал", "дешев", "упал", "упад",
             "просел", "сполз", "вниз", "минус")
_NEG_PRE = {"не", "ни"}
_CLAUSE_RE = re.compile(r"[.;!?…]|—")


def _word_hit(text_l, markers):
    """Маркер = префикс токена, В ПРЕДЕЛАХ КЛАУЗЫ: «не/ни» перед словом или «нет» сразу после —
    снимают маркер (перепроверка №3: «ставки НЕ выросли» читалось «вверх»); граница
    предложения рвёт связь (перепроверка №4: в «Роста нет. Упали цены» прежнее «нет» гасило
    чужое «упали» и давало переворот)."""
    for clause in _CLAUSE_RE.split(text_l):
        toks = re.findall(r"[а-яё]+", clause)
        for i, tok in enumerate(toks):
            if not any(tok.startswith(w) for w in markers):
                continue
            if i > 0 and toks[i - 1] in _NEG_PRE:
                continue
            if i + 1 < len(toks) and toks[i + 1] == "нет":
                continue
            return True
    return False
_PLAIN_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
# Перепроверка №2 02.11 (БЛОКЕР): наивное «последнее число слева vs первое справа» давало
# УВЕРЕННО ПЕРЕВЁРНУТЫЙ знак на живой библиотеке — пробельные тысячи («$10 000» → токены
# 10 и 000), дата в скобках слева от стрелки («(23.02.2022)» → операнд 2022), стрелка между
# словами («лаг объявление→пик»), рассогласованные единицы («$12 000→$17 тыс.»).
# Теперь: санитизация (скобки/даты вон, тысячи склеены) + операнды ТОЛЬКО вплотную к стрелке
# (между числом и стрелкой — лишь валюта/«/ед»/тильды) + бареные годы не операнды + единица
# «тыс/млн» на одной стороне → отказ. Любое сомнение → знак не утверждаем (fail-closed).
_PAREN_RE = re.compile(r"\([^)]*\)")
_DATE_TOK_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b")
_THOUSANDS_RE = re.compile(r"(?<=\d)[   ](?=\d{3}\b)")
_BETWEEN_OK_RE = re.compile(r"^[\s~≈$€£%]*(?:/[a-zа-яё]{1,4}\.?)?[\s~≈$€£%]*$")
_UNIT_AFTER_RE = re.compile(r"^\s*(?:тыс|млн|млрд|k)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:19|20)\d\d")


_OPERAND_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _interps(tok):
    """Возможные ЧИСЛОВЫЕ прочтения операнда. Запятая неоднозначна (перепроверка №4:
    глобальная склейка «X,YYY» читала русскую десятичную «0,875» как 875 и давала уверенный
    переворот): возвращаем ВСЕ прочтения, знак утверждается только если все комбинации
    согласны — иначе честный отказ (fail-closed)."""
    if "," not in tok:
        return {float(tok)} if tok.count(".") <= 1 else set()
    out = set()
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", tok):
        out.add(float(tok.replace(",", "")))           # англ. тысячи: 1,500 → 1500
    if tok.count(",") == 1 and "." not in tok:
        out.add(float(tok.replace(",", ".")))          # рус. десятичная: 1,25 → 1.25
    return out


def _arrow_sign(t):
    """Знак из сравнений «X → Y». Возвращает (вверх, вниз) — булевы вклады."""
    t = _THOUSANDS_RE.sub("", _DATE_TOK_RE.sub(" ", _PAREN_RE.sub(" ", t)))
    up = down = False
    for m in re.finditer(r"→|->", t):
        left, right = t[max(0, m.start() - 18):m.start()], t[m.end():m.end() + 18]
        lm = list(_OPERAND_RE.finditer(left))
        rm = list(_OPERAND_RE.finditer(right))
        if not lm or not rm:
            continue
        lo, ro = lm[-1], rm[0]
        if not (_BETWEEN_OK_RE.match(left[lo.end():]) and _BETWEEN_OK_RE.match(right[:ro.start()])):
            continue                               # между числом и стрелкой — чужие слова
        xs, ys = lo.group(0), ro.group(0)
        if any(_YEAR_RE.fullmatch(v) and not s.rstrip().endswith(("$", "€", "£"))
               for v, s in ((xs, left[:lo.start()]), (ys, right[:ro.start()]))):
            continue                               # барёный год — дата, не цена
        if _UNIT_AFTER_RE.match(right[ro.end():]):
            continue                               # «$17 тыс.» против «$12000» — единицы врозь
        signs = {(y > x) - (y < x) for x in _interps(xs) for y in _interps(ys)}
        if signs == {1}:
            up = True
        elif signs == {-1}:
            down = True                            # смешанные прочтения → знак не утверждаем
    return up, down


def _obs_sign(observed):
    t = str(observed or "").lower()
    up = "↑" in t
    down = "↓" in t
    up = up or _word_hit(t, _OBS_UP)
    down = down or _word_hit(t, _OBS_DOWN)
    # знак при числе: «+164%» → вверх, «−~40%» → вниз; дефис-диапазон «3-5» не знак
    for m in re.finditer(r"[+\-−]\s*[~≈]?\s*\d", t):
        prev = t[m.start() - 1] if m.start() > 0 else " "
        if m.group(0)[0] in "-−" and prev.isalnum():
            continue
        if m.group(0)[0] == "+":
            up = True
        else:
            down = True
    au, ad = _arrow_sign(t)
    up, down = up or au, down or ad
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
