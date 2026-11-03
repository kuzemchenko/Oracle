# -*- coding: utf-8 -*-
"""ops/hunter_seal.py — ПЕЧАТЬ идей охотника на СЕРВЕРЕ (П16) и «счёт метода» (архитектура 02.11).

Облачный охотник журналы не пишет (П16: журналы только с сервера). Он кладёт рядом с пушем
карточку idea_<дата>_<тикер>.card.json со §9-спекой; бот на сервере ПЕРЕД доставкой пуша
печатает её в journal/predictions.jsonl отдельным герметичным треком kind=hunter_idea
(к §11-деньгам не приближается), а в конец пуша код дописывает строку о печати и счёт метода.
Так идея охотника впервые попадает в контур честности: через 3–13 недель резолвер сверит
её с ценой закрытия, и владелец увидит «идеи охотника сбывались в X% (K из N)».

Сырая вероятность охотника идёт ТОЛЬКО в журнал для Brier — владельцу не печатается
(Инв#6: вероятность владельцу — из расчёта по треку, а не со слов LLM).
"""
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KIND = "hunter_idea"
TRACK = "hunter_idea"
# «Вето надо заработать» (разбор 02.11): кандидаты, отвергнутые судом охотника, печатаются ТЕНЬЮ
# в свой трек — владельцу не показываются; через 20+ сверенных видно, отличает ли суд плохое от
# хорошего (в прошлый раз разбитые судом сбывались в 52.6% — как все).
SHADOW_KIND = "hunter_killed"
MAX_SEAL_LAG_DAYS = 3        # П16: карточку старше 3 дней не печатаем — это было бы задним числом
DEDUP_FIELDS = ("track", "asset", "direction", "threshold", "resolve_by")
PRED = ROOT / "journal" / "predictions.jsonl"
OUTC = ROOT / "journal" / "outcomes.jsonl"
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")


def card_path_for(push_path):
    p = pathlib.Path(push_path)
    return p.with_name(p.name[:-len(".push.txt")] + ".card.json")


def spec_from_card(card, idea_file=None, kind=KIND):
    """Карточка охотника → прогноз для mathlib.sealing.seal (или ValueError с причиной)."""
    s = (card or {}).get("spec9") or {}
    try:
        thr = float(s.get("threshold"))
        prob = float(s.get("probability"))
    except (TypeError, ValueError):
        raise ValueError("в §9-спеке порог/вероятность не числа")
    rb = str(s.get("resolve_by") or "")[:10]
    datetime.date.fromisoformat(rb)                    # ValueError при битой дате
    asset = str(s.get("asset") or card.get("ticker") or "").upper()
    return {
        "kind": kind, "track": kind,
        "asset": asset, "direction": str(s.get("direction") or "").lower(),
        "threshold": thr, "resolve_by": rb + "T20:00:00+00:00",
        "price_source": s.get("price_source") or f"EODHD close {asset}",
        "probability": prob,
        "вероятность_источник": "охотник (LLM, сырая, не калибрована — владельцу не показывается)",
        "mechanism_id": card.get("mechanism_id"), "zone": card.get("zone"),
        "idea_file": idea_file,
        # перепроверка №2 (03.11): в записи — серверный якорь порога и цены идеи (чем мерили),
        # и БАЗА «случайного взгляда» (шанс того же закрытия от одной качки цены, кодом) — по ней
        # счёт метода отличает умение от шума
        "якорь_цена": s.get("last_close"), "якорь_дата": s.get("last_close_date"),
        "якорь_источник": s.get("цена_источник") or "карточка",
        "цена_повода": card.get("trigger_close"), "дата_повода": card.get("trigger_date"),
        "цель": card.get("target_price"), "стоп": card.get("stop_price"),
        "вход_не_хуже": card.get("entry_limit"),
        "вероятность_база": s.get("base_probability"),
        "катализатор_дата": (card.get("catalyst") or {}).get("date")
                            if isinstance(card.get("catalyst"), dict) else None,
        "spec_ref": "архитектура охоты 02.11; §9 разрешимость; П16 форвард-онли",
    }


BATTLE_PRED = ROOT / "journal" / "predictions.jsonl"


def _battle_write_allowed(target):
    """П16, инцидент 02.11 (дважды): тест и агент-проверяющий в облаке дописали печать в
    journal/predictions.jsonl ОБЛАЧНОЙ копии (вызов с путём по умолчанию). Печать в БОЕВОЙ журнал
    разрешена только на сервере (/home/oracle/…) или при явном ORACLE_ALLOW_JOURNAL=1; любой
    другой путь (временный в тестах) — свободно."""
    import os
    try:
        is_battle = target.resolve() == BATTLE_PRED.resolve()
    except OSError:
        is_battle = False
    if not is_battle:
        return True
    return (str(ROOT.resolve()).startswith("/home/oracle/")
            or os.environ.get("ORACLE_ALLOW_JOURNAL") == "1")


def _idea_stem(name):
    """idea_<дата>_<ТИКЕР>[.push.txt|.card.json|.shadow.json] → idea_<дата>_<ТИКЕР>."""
    n = str(name or "")
    for suf in (".push.txt", ".card.json", ".shadow.json", ".md"):
        if n.endswith(suf):
            return n[:-len(suf)]
    return n


_UP_RE = re.compile(r"(?<![а-яё])(рост|покупа|вверх|подорож|дорожа)", re.I)
_DOWN_RE = re.compile(r"(?<![а-яё])(снижени|падени|вниз|подешев|дешевеет|шорт|продаю\b)", re.I)
_BET_RE = re.compile(r"ставлю\s+на\s+([а-яё]+)", re.I)
_NUM_RE = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)?")


def _bet_side(header):
    """Сторона ставки по ЗАГОЛОВКУ: «ставлю на <слово>» → 'вверх'|'вниз'|None; без фразы — по
    словам-маркерам, только если они однозначны (перепроверка №2: поиск подстрок по всему тексту
    находил «рост» в «роста»/«просто» и пропускал перевёрнутый заголовок)."""
    if len(_BET_RE.findall(header)) > 1 or re.search(r"(?<![а-яё])не\s+ставлю", header, re.I):
        return None                                    # прицельная 03.11: «не ставлю на рост, ставлю на …»
    m = _BET_RE.search(header)
    if m:
        w = m.group(1)
        if _UP_RE.match(w):
            return "вверх"
        if _DOWN_RE.match(w):
            return "вниз"
    up, down = bool(_UP_RE.search(header)), bool(_DOWN_RE.search(header))
    if up != down:
        return "вверх" if up else "вниз"
    return None


def _has_number(text, value, rel=0.001):
    """Есть ли цена карточки в тексте — с точностью max(1 цент, 0.1%) (перепроверка №6: допуск 0.5%
    считал $47.30 «целью 47.5»)."""
    return _has_number_tol(text, value, rel)


def _has_number_tol(text, value, rel):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return True                                # в карточке нет числа — сверять нечего
    for tok in _NUM_RE.findall(text):
        try:
            x = float(tok.replace(",", "."))
        except ValueError:
            continue
        if v and abs(x - v) <= max(0.01, abs(v) * rel):
            return True
    return False


def push_card_mismatch(text, card, require_prices=True):
    """Текст пуша и §9-карточка говорят об одном? (перепроверка 02.11: суд и шлагбаум проверяли
    карточку, а владелец получал push.txt). Fail-closed: (1) тикер карточки есть в тексте;
    (2) ЗАГОЛОВОК прямо называет сторону ставки, и она = direction карточки; (3) цель и стоп
    карточки (цены) и дата сверки §9 есть в тексте — владелец видит ту же ставку, что в журнале.
    → причина | None."""
    t = str(text or "")
    card = card or {}
    base = str(card.get("ticker") or "").upper().split(".")[0]
    if base and not re.search(rf"(?<![A-Z]){re.escape(base)}(?![A-Z])", t):
        return f"в тексте нет тикера {base} из карточки"
    header = next((ln for ln in t.split("\n") if ln.strip()), "")
    d = str(card.get("direction") or "").strip().lower()
    side = _bet_side(header)
    if side is None:
        return "в заголовке не сказано однозначно, на что ставлю (рост/снижение)"
    if d in ("вверх", "вниз") and side != d:
        return f"карточка «{d}», а заголовок — ставка «{side}»"
    # перепроверка №6: и граница входа — деньги в абзаце кода считаются от неё
    for key, what in (("target_price", "цели"), ("stop_price", "стопа"), ("entry_limit", "границы входа")):
        if require_prices and card.get(key) is not None and not _has_number(t, card.get(key)):
            return f"в тексте нет {what} {card.get(key)} из карточки"
    if require_prices:
        # прицельная проверка 03.11: граница входа в «Что делать» обязана совпадать с карточкой —
        # деньги в абзаце кода считаются от неё; без entry_limit в карточке граница в тексте — брак
        todo = next((b for b in re.split(r"\n\s*\n", t) if _norm_head(b).startswith("что делать")), "")
        el = card.get("entry_limit")
        for m in re.finditer(r"не\s+(?:дороже|дешевле)\s+\$?\s?(\d+(?:[.,]\d+)?)", todo, re.I):
            x = float(m.group(1).replace(",", "."))
            if el is None:
                return f"в «Что делать» граница входа {m.group(1)}, а в карточке её нет (entry_limit)"
            if abs(x - float(el)) > max(0.01, 0.001 * float(el)):
                return f"в «Что делать» граница входа {m.group(1)}, а в карточке {el}"
    s9 = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    rb = str(s9.get("resolve_by") or "")[:10]
    if rb:
        try:
            dd = datetime.date.fromisoformat(rb)
        except ValueError:
            return "в карточке битая дата сверки"
        # перепроверка №3: подстрокой «5 ноября» находилось внутри «25 ноября» — с границей числа
        if not (re.search(rf"(?<!\d){dd.day}\s+{_MONTHS[dd.month - 1]}", t)
                or re.search(rf"(?<!\d)0?{dd.day}\.{dd.month:02d}(?!\d)", t)):
            return f"в тексте нет даты сверки {dd.day} {_MONTHS[dd.month - 1]} из карточки"
    return None


# ── Абзацы с ДЕНЬГАМИ пишет КОД (перепроверка №4) ─────────────────────────────────────────────
# Раньше: модель писала ход с повода и «±$ на $500», а код пытался их разобрать и сверить —
# хрупко в обе стороны (первый % абзаца мог быть прецедентом, тире диапазона читалось минусом,
# перевёрнутый знак проходил) и сверялось с ценами LLM, а не сервера. Теперь абзацы «Почему это
# ещё не в цене» и «Сколько можно заработать и чем рискую» бот ЗАМЕНЯЕТ текстом из расчёта кода
# по серверным ценам (Инв#6); модель пишет механизм и аргументы, код — цифры.
WHY_HEAD = "Почему это ещё не в цене."
MONEY_HEAD = "Сколько можно заработать и чем рискую."
TODO_HEAD = "Что делать."


def _norm_head(t):
    return re.sub(r"\s+", " ", str(t or "").strip().lower().replace("ё", "е"))


def _ratio_words(r):
    """Асимметрия СЛОВАМИ, без цифр (перепроверка №5: «в 1,7 раза» добавляло число и на сервере, при
    другой цене, валило лимит линта ≤8 чисел). Точное попадание (±0.05) — «в полтора раза», иначе
    «больше чем в …» по ближайшей снизу половинке."""
    names = {1.5: "в полтора раза", 2.0: "вдвое", 2.5: "в два с половиной раза", 3.0: "втрое",
             3.5: "в три с половиной раза", 4.0: "вчетверо", 5.0: "впятеро"}
    for k, w in names.items():
        if k - 1e-6 <= r <= k + 0.05:                  # допуск как у шлагбаума; 1.46 — не «в полтора раза»
            return w
    below = [k for k in names if k < r]
    if not below:
        return None                                    # меньше полутора — словами не хвалим
    return "больше чем " + names[max(below)]


def _ratio_sentence(r):
    w = _ratio_words(r)
    if w is None:
        return None
    if w.startswith("больше чем"):
        return f"Выигрыш превышает возможную потерю {w}."
    return f"Выигрыш {w} больше возможной потери."


def _round5(x):
    return int(5 * round(float(x) / 5.0))


def _phase_text(verdict, direction="вверх"):
    """Фаза «что уже в цене» популярным языком (просьба владельца 03.11: «писать понятно
    человеку с высшим образованием», без «механизма» и «хода»)."""
    from mathlib import priced_in as PI
    move = "рост" if direction == "вверх" else "падение"
    gen = "роста" if direction == "вверх" else "падения"
    return {PI.ПРОТИВ: f"пока цена шла в обратную сторону — {move}, как в похожем случае, ещё не начинался"
                       if move == "рост" else
                       f"пока цена шла в обратную сторону — {move}, как в похожем случае, ещё не начиналось",
            PI.РАНО: f"из {gen}, как в похожем случае, пройдена малая часть",
            PI.В_ПУТИ: f"часть {gen}, как в похожем случае, уже пройдена",
            PI.В_ЦЕНЕ: f"большая часть {gen}, как в похожем случае, уже пройдена",
            PI.ПЕРЕЛЁТ: f"цена уже ушла дальше, чем в похожем случае"}.get(verdict, "")


def render_with_problems(card, update=False):
    """Карточка с пересчитанными ценами (после reprice) → ({'why','money'}, проблемы).
    Числа ровно по шаблону: «почему» — ОДНО число всегда, «деньги» — ЧЕТЫРЕ (стабильный счёт для
    линта и в облаке, и на сервере). Новая идея в состоянии «в цене / перелёт / цена за стопом / цель
    взята / асимметрия < 1.5» — проблема (не печатать, не слать). Исправление в таком состоянии —
    честный текст «план больше не действует», без «+$90» (перепроверка №5)."""
    try:
        from ops import idea_gate as G
    except ImportError:
        import idea_gate as G
    from mathlib import priced_in as PI
    s9 = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    d = str(card.get("direction") or "").strip().lower()
    pm = G.price_math(d, s9.get("last_close"), card.get("trigger_close"), card.get("target_price"),
                      card.get("stop_price"), card.get("entry_limit"))
    out, problems = {}, []
    mv = pm.get("ход_с_повода")
    td = G._date(card.get("trigger_date"))
    if mv is None or td is None or d not in ("вверх", "вниз"):
        problems.append("ход с даты повода не посчитан кодом — абзац «Почему это ещё не в цене» не из чего писать")
    else:
        n = int(abs(mv) + 0.5)                         # округление «как в школе», без банковского
        verb = ("почти не сдвинулась (меньше 1%)" if n == 0 else
                f"подешевела на {n}%" if mv < 0 else f"подорожала на {n}%")
        what = str(card.get("trigger_what") or "").strip().rstrip(".")
        since = f"С {what} {td.day} {_MONTHS[td.month - 1]}" if what else f"С {td.day} {_MONTHS[td.month - 1]}"
        phase, verdict = "", None
        ranges = PI.parse_amplitude(str((card.get("precedent") or {}).get("amplitude") or ""))
        if ranges:
            lo, hi = sorted((abs(ranges[0]["низ"]), abs(ranges[0]["верх"])))
            pf = PI.played_fraction(mv if d == "вверх" else -mv, f"+{lo:g}–{hi:g}%" if lo != hi else f"+{hi:g}%")
            verdict = pf.get("вердикт")
            phase = _phase_text(verdict, d)
        if verdict in (PI.В_ЦЕНЕ, PI.ПЕРЕЛЁТ) and not update:
            problems.append(f"звено уже отыграно ({verdict}) — новая идея не уходит")
        out["why"] = f"{WHY_HEAD} {since} бумага {verb} по ценам закрытия" + (f": {phase}." if phase else ".")
    g, l = pm.get("потенциал_от_входа"), pm.get("риск_от_входа")
    # прицельная проверка 03.11: отношение — НЕокруглённое, ровно как в шлагбауме (асимметрия 1.5
    # проходила шлагбаум и бракoвалась рендером по округлённым процентам)
    ratio = pm.get("асимметрия")
    sentence = _ratio_sentence(ratio) if ratio is not None else None
    broken = [f for f in pm.get("fails") or []]
    # прицельная проверка 03.11: граница входа — ЛИМИТ («не дороже $X»), а не вход сейчас; рынок выше
    # границы план не ломает (особенно у «Идеи с триггером», где вход в день развилки) — ломают план
    # только стоп, взятая цель и асимметрия < 1.5 от границы. Большой отрыв ждёт в server_check.
    if broken or sentence is None:
        if update:
            why_not = ("цена уже за стопом" if any("за стопом" in f for f in broken) else
                       "цель уже достигнута" if any("достигнута" in f for f in broken) else
                       "возможный выигрыш больше не превышает возможную потерю в полтора раза")
            out["money"] = (f"{MONEY_HEAD} По последней цене закрытия прежний план уже не действует: "
                            f"{why_not}.")
            rb = G._date(s9.get("resolve_by"))
            when = f" после {rb.day} {_MONTHS[rb.month - 1]}" if rb else " в свой срок"
            out["todo"] = (f"{TODO_HEAD} По этому плану сейчас не входить: {why_not}. Записанный прогноз не "
                           f"меняется и будет сверен{when}.")
        else:
            problems.append("деньги не посчитать кодом: " + ("; ".join(broken) if broken else
                            "выигрыш меньше полутора возможных потерь"))
    else:
        basis = (("если войти по самой высокой цене из плана" if d == "вверх" else
                  "если войти по самой низкой цене из плана") if card.get("entry_limit")
                 else "если войти по последней цене закрытия")
        out["money"] = (f"{MONEY_HEAD} На пятьсот долларов на бумаге, {basis}: прав — около +${_round5(5 * g)} "
                        f"(+{g:.0f}%), неправ — около −${_round5(5 * l)} (−{l:.0f}%). {sentence}")
        cs = chance_sentence(card)
        if cs:
            out["money"] += " " + cs
    return out, problems


def chance_sentence(card):
    """Решение владельца 03.11 («да, показывай шанс»): шанс цели — ИЗ КОДА (база «случайного взгляда»
    mathlib/base_odds по σ серверных закрытий, считается при reprice), не со слов модели (Инв#6).
    Без серверной базы (облачный предпросмотр) — строки нет."""
    s9 = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    bp = s9.get("base_probability")
    try:
        n = int(round(float(bp) * 100))
    except (TypeError, ValueError):
        return None
    rb = str(s9.get("resolve_by") or "")[:10]
    try:
        d = datetime.date.fromisoformat(rb)
        when = f" к {d.day} {_MONTHS[d.month - 1]}"
    except ValueError:
        when = ""
    if n < 1:
        n_txt = "меньше чем в 1 случае из 100"
    else:
        n_txt = f"примерно в {n} случаях из 100"
    return (f"Шанс дойти до цели{when}: просто от обычных колебаний цены такое бывает {n_txt} — "
            "ставка в том, что этот повод даст больше обычного.")


def render_numbers(card, update=False):
    return render_with_problems(card, update=update)[0]


def apply_rendered(text, rendered, drop_unrendered=True):
    """Заменить абзацы «Почему это ещё не в цене» и «Сколько можно заработать» текстом кода;
    нет такого абзаца — вставить перед «Кто продаёт…» (или перед «Что делать», или в конец).
    Черновой абзац, для которого кодового нет, УДАЛЯЕТСЯ (drop_unrendered): числа модели владельцу
    не уходят ни при каких условиях (перепроверка №5)."""
    rendered = rendered or {}
    blocks = re.split(r"\n\s*\n", str(text).strip())
    heads = {"why": _norm_head(WHY_HEAD)[:20], "money": _norm_head(MONEY_HEAD)[:20]}
    if "todo" in rendered:                             # «план не действует»: черновой план не остаётся
        heads["todo"] = "что делать"                   # любой вариант заголовка («Что делать:», «— …»)
    done, keep = set(), []
    for b in blocks:
        nb = _norm_head(b)
        hit = next((k for k, h in heads.items() if nb.startswith(h)), None)
        if hit is None:
            keep.append(b)
        elif hit in rendered:
            # строки-рамка «исследовательский инструмент…» в заменяемом абзаце сохраняются (§16)
            frame = [ln for ln in b.split("\n") if "исследовательский инструмент" in ln]
            if hit not in done:                        # дубль заголовка после подстановки — выбрасываем
                keep.append(rendered[hit] + ("\n" + "\n".join(frame) if frame else ""))
                done.add(hit)
            elif frame:
                keep.append("\n".join(frame))
        elif not drop_unrendered:
            keep.append(b)
    blocks = keep
    missing = [rendered[k] for k in ("why", "money", "todo") if k in rendered and k not in done]
    if missing:
        anchor = next((i for i, b in enumerate(blocks) if _norm_head(b).startswith("кто продает")), None)
        if anchor is None:
            anchor = next((i for i, b in enumerate(blocks) if _norm_head(b).startswith("что делать")), len(blocks))
        blocks[anchor:anchor] = missing
    return "\n\n".join(blocks)


def prepare_push(text, card, update=False):
    """ОДНА проверка для бота и облачного предпросмотра (перепроверка №5): собрать ИТОГОВЫЙ текст
    (абзацы с деньгами — кодом) и проверить ИМЕННО его — сверка с карточкой (тикер, сторона ставки,
    цель/стоп/дата) и линт подачи. Вызывается ДО суда и ДО печати. → (итоговый текст, проблемы)."""
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    rendered, problems = render_with_problems(card, update=update)
    final = apply_rendered(text, rendered)
    mm = push_card_mismatch(final, card, require_prices="todo" not in rendered)
    if mm:
        problems.append("текст не согласован с карточкой: " + mm)
    # строка шанса — из КОДА (решение владельца 03.11): запрет вероятностных слов и счёт чисел
    # линта стоят против слов МОДЕЛИ, поэтому кодовая строка в линт не идёт
    cs = chance_sentence(card)
    problems += [str(v) for v in PL.lint_hunter_idea(final.replace(" " + cs, "") if cs else final)]
    return final, problems


def preview(stem, today=None):
    """Облако и сервер видят ОДНО и то же: prepare_push по карточке (в облаке — её цены, на сервере —
    серверные). stem = reports/ideas/idea_<дата>_<ТИКЕР>. → {'problems', 'text'}; код выхода CLI 0 —
    только если проблем нет."""
    text = pathlib.Path(str(stem) + ".push.txt").read_text(encoding="utf-8").strip()
    card = json.loads(pathlib.Path(str(stem) + ".card.json").read_text(encoding="utf-8"))
    final, problems = prepare_push(text, card)
    gap = entry_gap_problem(card)
    if gap:
        problems = list(problems) + [gap]
    return {"problems": problems, "text": final}


def entry_gap_problem(card):
    """Облачная проверка того же правила, что server_check (ENTRY_GAP): рынок дальше 5% за границей
    входа — сервер придержит идею (повтор раз в час) и после MAX_SEAL_LAG_DAYS дней откажет. 03.11 DHT
    прошёл preview, а на сервере застрял: «цена 23.86 далеко за границей входа 22.6». → причина | None."""
    s9 = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    try:
        px, el = float(s9.get("last_close")), float(card.get("entry_limit"))
    except (TypeError, ValueError):
        return None
    if px <= 0 or el <= 0:
        return None
    dd = str(card.get("direction") or "").strip().lower()
    if (dd == "вверх" and px > el * (1 + ENTRY_GAP)) or (dd == "вниз" and px < el * (1 - ENTRY_GAP)):
        return (f"граница входа {el:g} дальше {ENTRY_GAP:.0%} от последнего закрытия {px:g}: сервер придержит "
                f"идею и доставит, только если рынок подойдёт к входу в течение {MAX_SEAL_LAG_DAYS} дней с "
                "охоты, иначе откажет — сдвинь вход к цене или дай идею, исполнимую сейчас")
    return None


def sealed_record(idea_file, predictions_path=None, kind=KIND):
    """Запечатанная запись этой идеи (по стему) или None."""
    stem = _idea_stem(idea_file)
    recs = [r for r in _read_jsonl(predictions_path or PRED)
            if r.get("kind") == kind and _idea_stem(r.get("idea_file")) == stem]
    return recs[-1] if recs else None


def update_bet_mismatch(card, idea_file, predictions_path=None):
    """Исправление доставленной идеи не вправе менять ЗАПЕЧАТАННУЮ ставку (перепроверка №2:
    футер говорил «уже записан», а текст нёс другой порог/срок). → причина | None."""
    rec = sealed_record(idea_file, predictions_path)
    if rec is None:
        return None
    try:
        spec = spec_from_card(card, idea_file=idea_file)
    except ValueError as e:
        return f"спека исправления не разрешима: {e}"
    for k in ("asset", "direction"):
        if str(spec.get(k) or "").upper() != str(rec.get(k) or "").upper():
            return f"исправление меняет {k}: в журнале {rec.get(k)}, в карточке {spec.get(k)}"
    # перепроверка №6 (high): spec9 мог остаться прежним, а сторона идеи — перевернуться
    side = {"вверх": "above", "вниз": "below"}.get(str(card.get("direction") or "").strip().lower())
    if side != str(rec.get("direction") or "").lower():
        return (f"исправление меняет сторону идеи: в журнале {rec.get('direction')}, "
                f"в карточке «{card.get('direction')}»")
    # прицельная проверка 03.11: и цена плана (цель, стоп, граница входа) — как в записи печати
    for ck, rk, what in (("target_price", "цель", "цель"), ("stop_price", "стоп", "стоп"),
                         ("entry_limit", "вход_не_хуже", "границу входа")):
        a, b = card.get(ck), rec.get(rk)
        if b is None:
            continue
        try:
            if a is None or abs(float(a) - float(b)) > max(0.01, 0.001 * abs(float(b))):
                return f"исправление меняет {what}: в журнале {b}, в карточке {a}"
        except (TypeError, ValueError):
            return f"исправление меняет {what}: в журнале {b}, в карточке {a}"
    if abs(float(spec["threshold"]) - float(rec.get("threshold") or 0)) > 1e-9:
        return f"исправление меняет порог: в журнале {rec.get('threshold')}, в карточке {spec['threshold']}"
    if str(spec["resolve_by"])[:10] != str(rec.get("resolve_by") or "")[:10]:
        return (f"исправление меняет дату сверки: в журнале {str(rec.get('resolve_by'))[:10]}, "
                f"в карточке {str(spec['resolve_by'])[:10]}")
    return None


def idea_sealed(idea_file, predictions_path=None, kind=KIND):
    """Есть ли уже печать по этой идее (по стему имени файла)."""
    stem = _idea_stem(idea_file)
    return any(r.get("kind") == kind and _idea_stem(r.get("idea_file")) == stem
               for r in _read_jsonl(predictions_path or PRED))


TRANSIENT = "ПОВТОР: "          # префикс причины временного сбоя: не брак, повторить позже
MAX_TRIGGER_CLOSE_DIFF = 0.03   # цена в день повода в карточке против котировок сервера: допуск 3%
ENTRY_GAP = 0.05                # рынок дальше 5% за границей входа — новая идея ждёт (лимит не исполним)


def is_transient(fails):
    """Причина — временный сбой данных (нет/устарела серверная цена), а не брак идеи."""
    return bool(fails) and str(fails[0]).startswith(TRANSIENT)


def _server_prices(asset, trigger_date=None, con=None):
    """СЕРВЕРНЫЕ котировки для шлагбаума (Инв#6: не со слов LLM) с добором истории бумаги вне
    базы (data/eodhd.ensure_history), если есть ключ EODHD. → {'last_close', 'last_date',
    'trigger_close', 'trigger_date', 'closes'} | None (нет базы/строк — временный сбой)."""
    import os
    import sqlite3
    db = ROOT / "storage" / "oracle.db"
    own = con is None
    try:
        if con is None:
            if not db.exists():
                return None
            con = sqlite3.connect(db, timeout=30)
        key = os.environ.get("EODHD_API_KEY")
        if key:
            try:
                sys.path.insert(0, str(ROOT / "data"))
                import eodhd as E
                E.ensure_history(con, [asset], key)
            except Exception:                          # noqa: BLE001 — добор best-effort
                pass
        rows = con.execute("SELECT date, close FROM quotes WHERE symbol=? AND close IS NOT NULL "
                           "ORDER BY date DESC LIMIT 61", (asset,)).fetchall()
        if not rows:
            return None
        out = {"last_close": float(rows[0][1]), "last_date": str(rows[0][0])[:10],
               "closes": [float(r[1]) for r in reversed(rows)], "trigger_close": None,
               "trigger_date": None}
        if trigger_date:
            tr = con.execute("SELECT date, close FROM quotes WHERE symbol=? AND close IS NOT NULL "
                             "AND date<=? ORDER BY date DESC LIMIT 1",
                             (asset, str(trigger_date)[:10])).fetchone()
            if tr:
                d = datetime.date.fromisoformat(str(tr[0])[:10])
                if (datetime.date.fromisoformat(str(trigger_date)[:10]) - d).days <= 5:
                    out["trigger_close"], out["trigger_date"] = float(tr[1]), str(tr[0])[:10]
        return out
    except Exception:                                  # noqa: BLE001 — «база занята» и пр. — временно
        return None
    finally:
        if own and con is not None:
            con.close()


def server_check(card, *, idea_file, today=None, last_close_fn=None, prices_fn=None):
    """ШЛАГБАУМ КОДОМ НА СЕРВЕРЕ (ревью 02.11: в облаке он исполнялся на слово LLM). Карточка
    перепроверяется ops/idea_gate.check с СЕРВЕРНОЙ датой; цены (последнее закрытие, закрытие в
    день повода) — из серверных котировок, проценты хода/потенциала/риска пересчитываются по ним
    КОДОМ (перепроверка №2 03.11: раньше подменялась только цена закрытия, а «что в цене» и
    асимметрия считались по числам LLM); горизонт меряется от даты охоты (лаг доставки ≤3 дн).
    Нет/устарели серверные котировки → причина с префиксом TRANSIENT (повторить позже, не брак).
    → (ok, причины, карточка_с_серверными_ценами)."""
    import copy
    try:
        from ops import idea_gate as G
    except ImportError:
        import idea_gate as G
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    hd = hunt_date_from_name(idea_file)
    if hd is None:
        return False, ["в имени файла нет даты охоты — возраст карточки не проверить"], card
    if hd > today:                                   # перепроверка №3: будущая дата снимала лаг-гард
        return False, [f"дата охоты {hd} в имени файла позже сегодняшней {today}"], card
    if (today - hd).days > MAX_SEAL_LAG_DAYS:
        return False, [f"карточка от {hd} старше {MAX_SEAL_LAG_DAYS} дней — задним числом не печатаем"], card
    c2, fails = reprice(card, today=today, last_close_fn=last_close_fn, prices_fn=prices_fn)
    if fails:
        return False, fails, c2
    s9c = c2.get("spec9") if isinstance(c2.get("spec9"), dict) else {}
    px, el = G._num(s9c.get("last_close")), G._num(c2.get("entry_limit"))
    dd = str(c2.get("direction") or "").strip().lower()
    if px and el and ((dd == "вверх" and px > el * (1 + ENTRY_GAP)) or (dd == "вниз" and px < el * (1 - ENTRY_GAP))):
        # перепроверка №6 + прицельная 03.11: рынок далеко (>5%) за границей входа — лимит вряд ли
        # исполнится и §9-вопрос от рынка мерил бы не то, что показано; цена может вернуться — повтор
        # (окно печати 3 дня ограничит ожидание). Небольшой отрыв — норма лимитного входа.
        return False, [TRANSIENT + f"цена {px:g} далеко за границей входа {el:g} — жду, пока план станет исполним"], c2
    res = G.check(c2, today=today, horizon_from=hd)
    return res["pass"], res["fails"], c2


def reprice(card, *, today=None, last_close_fn=None, prices_fn=None):
    """Карточка → (копия с СЕРВЕРНЫМИ ценами и пересчитанными кодом процентами, причины отказа).
    Общая часть шлагбаума новой идеи и пути «исправление доставленной» (перепроверка №4: числа,
    которые видит владелец, обязаны быть серверными и в исправлениях). Нет/устарели котировки →
    причина с префиксом TRANSIENT; цена в день повода в карточке ≠ котировкам (>3%) — брак."""
    import copy
    try:
        from ops import idea_gate as G
    except ImportError:
        import idea_gate as G
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    c2 = copy.deepcopy(card)
    s = c2.get("spec9") if isinstance(c2.get("spec9"), dict) else None
    if s is None:
        return c2, ["в карточке нет §9-спеки объектом"]
    asset = str(s.get("asset") or c2.get("ticker") or "").upper()
    if prices_fn is None and last_close_fn is not None:   # тесты: только цена закрытия
        px, pxd = last_close_fn(asset)
        pr = None if px is None else {"last_close": px, "last_date": pxd, "closes": None,
                                      "trigger_close": c2.get("trigger_close"),
                                      "trigger_date": c2.get("trigger_date")}
    else:
        pr = (prices_fn or _server_prices)(asset, c2.get("trigger_date"))
    if not pr or pr.get("last_close") is None:
        # без серверной цены якорь/сторону/свежесть проверить нечем — цена со слов LLM не основание
        # для печати (Инв#6); но это СБОЙ ДАННЫХ, а не брак идеи — повтор позже
        return c2, [TRANSIENT + f"нет серверной цены закрытия {asset} — якорь порога не проверить"]
    px, pxd = float(pr["last_close"]), str(pr["last_date"])[:10]
    age = (today - datetime.date.fromisoformat(pxd)).days
    if age > G.MAX_PRICE_AGE_DAYS:
        return c2, [TRANSIENT + f"серверная цена {asset} от {pxd} устарела ({age} дн) — котировки "
                    "не обновились"]
    s["last_close"], s["last_close_date"] = px, pxd
    s["цена_источник"] = "серверные котировки EODHD"
    if c2.get("trigger_date"):
        tcs = pr.get("trigger_close")
        if tcs is None:
            return c2, [TRANSIENT + f"нет серверного закрытия {asset} на дату повода {c2.get('trigger_date')}"]
        tcc = G._num(c2.get("trigger_close"))
        if tcc and abs(tcc / float(tcs) - 1) > MAX_TRIGGER_CLOSE_DIFF:
            return c2, [f"цена в день повода в карточке {tcc:g}, а по котировкам сервера "
                        f"{float(tcs):g} — автор ошибся в исходной цене"]
        c2["trigger_close"] = float(tcs)
    pm = G.price_math(str(c2.get("direction") or "").lower(), px, c2.get("trigger_close"),
                      c2.get("target_price"), c2.get("stop_price"), c2.get("entry_limit"))
    for key, calc in (("move_since_trigger_pct", "ход_с_повода"), ("upside_pct", "потенциал"),
                      ("downside_pct", "риск")):
        if pm.get(calc) is not None:
            c2[key] = pm[calc]
    s["base_probability"] = _base_probability(s, pr.get("closes"), pxd)
    return c2, []


def _base_probability(spec, closes, last_date):
    """База «случайного взгляда» (mathlib.base_odds) для запечатываемого вопроса или None."""
    try:
        from mathlib import base_odds as BO
        sig = BO.daily_sigma(closes or [])
        days = BO.trading_days(last_date, str(spec.get("resolve_by"))[:10])
        p = BO.base_probability(spec.get("last_close"), spec.get("threshold"), sig, days,
                                str(spec.get("direction") or "").lower()) if sig else None
        return round(p, 4) if p is not None else None
    except Exception:                                  # noqa: BLE001 — база не обязательна для печати
        return None


def hunt_date_from_name(name):
    """idea_<YYYY-MM-DD>_<ТИКЕР>.* → date | None."""
    try:
        return datetime.date.fromisoformat(str(name)[len("idea_"):len("idea_") + 10])
    except ValueError:
        return None


def seal_card(card, *, idea_file=None, predictions_path=None, sealable_fn=None, kind=KIND,
              today=None):
    """→ (record|None, причина|None). Дубль ставки → (None, 'уже запечатано') — не ошибка.
    П16: карточка старше MAX_SEAL_LAG_DAYS от даты охоты (из имени файла) не печатается —
    за это время цена уже ушла, печать была бы задним числом."""
    from mathlib import sealing as SEAL
    target = pathlib.Path(predictions_path or PRED)
    if not _battle_write_allowed(target):
        return None, "печать в боевой журнал разрешена только на сервере"
    hd = hunt_date_from_name(idea_file) if idea_file else None
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    if hd is None:                                  # ревью 02.11: без даты лаг-гард был fail-open
        return None, "в имени карточки нет даты охоты — возраст не проверить, печать отклонена"
    if hd > today:
        return None, f"дата охоты {hd} позже сегодняшней — печать отклонена"
    if (today - hd).days > MAX_SEAL_LAG_DAYS:
        return None, (f"карточка от {hd} старше {MAX_SEAL_LAG_DAYS} дней — печать задним числом "
                      "запрещена")
    stem = _idea_stem(idea_file)
    recs = _read_jsonl(predictions_path or PRED)
    if any(r.get("kind") == kind and _idea_stem(r.get("idea_file")) == stem for r in recs):
        return None, "уже запечатано"               # одна печать на идею (и при «обновлённой версии»)
    asset = str(((card or {}).get("spec9") or {}).get("asset") or (card or {}).get("ticker") or "").upper()
    open_same = [r for r in recs if r.get("kind") == kind and str(r.get("asset") or "").upper() == asset
                 and str(r.get("resolve_by") or "")[:10] >= today.isoformat()]
    if open_same:                                   # коррелированные исходы не считаем независимыми
        return None, (f"по этой бумаге уже идёт прогноз до {str(open_same[-1].get('resolve_by'))[:10]}"
                      " — второй до сверки первого не открываю")
    try:
        spec = spec_from_card(card, idea_file=idea_file, kind=kind)
    except ValueError as e:
        return None, f"спека не разрешима: {e}"
    if sealable_fn is None:
        try:
            from orchestrator import universe_resolver as U
            sealable_fn = U.is_sealable
        except Exception:                              # noqa: BLE001
            sealable_fn = None
    if sealable_fn is not None and not sealable_fn(spec["asset"]):
        return None, f"нет истории цен {spec['asset']} в базе — сверять нечем (§9)"
    try:
        rec = SEAL.seal(spec, path=predictions_path or PRED, dedup_fields=DEDUP_FIELDS)
    except ValueError as e:
        return None, str(e)
    if rec is None:
        return None, "уже запечатано"
    return rec, None


def _read_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def method_score(predictions_path=None, outcomes_path=None, kind=KIND):
    """Счёт МЕТОДА охотника из журналов (П16: только из журнала, join по hash). Перепроверка №2:
    голое «K из N» без базы ничего не значит — рядом сумма БАЗ «случайного взгляда» по тем же
    сверенным вопросам (сколько сбылось бы от одной качки цены; mathlib.base_odds при печати)."""
    preds = [p for p in _read_jsonl(predictions_path or PRED) if p.get("kind") == kind]
    outs = {o.get("hash"): o for o in _read_jsonl(outcomes_path or OUTC)
            if o.get("hash") and o.get("outcome") in (0, 1)}
    pairs = [(p, outs[p["hash"]]) for p in preds if p.get("hash") in outs]
    based = [(p, o) for p, o in pairs if isinstance(p.get("вероятность_база"), (int, float))]
    ps = [float(p["вероятность_база"]) for p, _o in based]
    kb = sum(int(o["outcome"]) for _p, o in based)
    try:
        from mathlib import base_odds as BO
        pval = BO.poisson_binomial_sf(kb, ps) if ps else None
    except Exception:                                  # noqa: BLE001
        pval = None
    return {"идей": len(preds), "исходов": len(pairs),
            "попаданий": sum(int(o["outcome"]) for _p, o in pairs),
            "с_базой": len(based), "база_ожидание": round(sum(ps), 2),
            "попаданий_с_базой": kb, "p_шум": pval}


def score_line(score):
    """Строка «счёт метода» для владельца: K из N и база шума на тех же порогах (кодом)."""
    n, k, total = score.get("исходов", 0), score.get("попаданий", 0), score.get("идей", 0)
    if n == 0:
        return (f"Счёт метода охотника: идей в журнале {total}, сверенных пока нет — "
                "первые исходы через 3–13 недель.")
    line = f"Счёт метода охотника: сбылось {k} из {n} сверенных"
    nb, eb, kb = score.get("с_базой", 0), score.get("база_ожидание"), score.get("попаданий_с_базой", 0)
    if nb:
        # перепроверка №3: сравниваем ОДНУ популяцию — идеи с посчитанной базой — и говорим словами,
        # отличимо ли это от шума (хвост Пуассона-биномиального распределения, mathlib/base_odds.py)
        line += ("; " if nb == n else f"; из {nb} с посчитанной базой сбылось {kb}, а ")
        line += f"от одной обычной качки цен на тех же порогах сбылось бы около {eb:g}"
        pv = score.get("p_шум")
        if nb >= 30 and pv is not None:                 # §10: выводы — от N≥30
            line += (" — это чаще, чем бывает случайно" if pv < 0.05 and kb > eb else
                     " — пока не отличить от случайности" if kb > eb else " — пока не лучше шума")
    return line + (" — выборка мала, доверять рано." if n < 30 else ".")


def _human_date(iso):
    d = datetime.date.fromisoformat(str(iso)[:10])
    return f"{d.day} {_MONTHS[d.month - 1]}"


def footer(rec, why, score):
    """Строки, которые КОД дописывает в пуш: что запечатано и счёт метода (без вероятностей
    генератора). Перепроверка №2: прогноз — ВОПРОС, который сверю, а не утверждение «закроется»."""
    lines = []
    if rec:
        side = "выше" if rec["direction"] == "above" else "ниже"
        base = rec["asset"].split(".")[0]
        # разбор 02.11: прогноз проверяет ВЗГЛЯД метода на бумагу, а не сделку владельца (вошёл
        # он или нет, по условию или нет) — говорим это прямо, чтобы счёт и деньги не путались
        lines.append(f"Записал в журнал проверку: закроется ли {base} {side} {rec['threshold']:g} "
                     f"к {_human_date(rec['resolve_by'])}. Это проверка взгляда метода на бумагу, "
                     "а не твоей сделки: после этой даты сверю по цене закрытия и пришлю итог сюда — "
                     "да или нет; запись задним числом не правится.")
    elif why == "уже запечатано":
        lines.append("Проверяемый прогноз по этой идее уже записан в журнал раньше — сверю в срок.")
    elif why:
        plain = re.sub(r"\s*\((?:§|П)\d+[^)]*\)", "", str(why))
        plain = re.sub(r"\b([A-Z][A-Z0-9\-]{0,9})\.US\b", r"\1", plain).strip().rstrip(".")
        lines.append(f"Счёт по этой идее не веду: {plain}.")
    lines.append(score_line(score))
    return lines


def outcome_message(outcome, score):
    """Итог сверки идеи охотника → сообщение владельцу (обратная связь «сбылось/нет»)."""
    base = str(outcome.get("asset") or "?").split(".")[0]
    side = "выше" if outcome.get("direction") == "above" else "ниже"
    obs = outcome.get("observed_value")
    ok = int(outcome.get("outcome")) == 1
    when = _human_date(outcome.get("resolve_by")) if outcome.get("resolve_by") else "дату сверки"
    obs_txt = f"{float(obs):g}" if isinstance(obs, (int, float)) else "нет данных"
    return (f"Итог идеи: проверял, закроется ли {base} {side} {float(outcome.get('threshold')):g} "
            f"к {when}. Закрытие — {obs_txt}: {'сбылось' if ok else 'не сбылось'}.\n"
            + score_line(score))



if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Предпросмотр пуша охотника так, как его отправит бот")
    ap.add_argument("cmd", choices=["preview"])
    ap.add_argument("stem", help="reports/ideas/idea_<дата>_<ТИКЕР> (без расширения)")
    a = ap.parse_args()
    res = preview(a.stem)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    sys.exit(0 if not res["problems"] else 1)
