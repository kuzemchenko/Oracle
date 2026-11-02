# -*- coding: utf-8 -*-
"""ops/presentation_lint.py — Этап3 (пакет после аудита): линтер СТИЛЬ-КОНТРАКТА артефактов.

Реализует правило STYLE_CONTRACT.md (§6): линтер ВАЛИТ сборку/пуш на нарушении контракта. Держит
дисциплину «Разбора дня» и любого текста для владельца: ноль эмодзи, обязательные секции,
расшифровка жаргона при первом употреблении. Чистая функция (без I/O) — тестируемо и вызываемо
из бота ПЕРЕД отправкой (fail-closed: сломанный шаблон не уходит владельцу).

check_daily_case(text) → [нарушения]; пусто = чисто. lint_for_user(text, *, require_sections)
— общий проход (эмодзи + жаргон + опц. секции) для прочих артефактов.
"""
import re
import unicodedata

# Жаргон из стоп-списка STYLE_CONTRACT п.2: при ПЕРВОМ употреблении обязана быть расшифровка в
# одну строку. Регистронезависимо; для латиницы — границы слова, чтобы не ловить подстроки.
STOPLIST = ["q-value", "fdr", "z-score", "tier", "ярус", "каскад", "brier", "σ", "r²",
            # испытатель №1 (01.11): жаргон, доходивший до владельца сырым
            "чокпоинт", "edge", "первого порядка", "второго порядка", "лонг", "шорт"]

# Маркеры расшифровки рядом с термином — «(…)», тире-расшифровка, «то есть». Ищем ТОЛЬКО в пределах
# той же фразы (до ближайшего . ! ? или перевода строки), иначе далёкое «что это» ложно засчитается.
_GLOSS = ("(", " — ", " – ", "—", "–", "то есть", ", что ")

# Обязательные секции «Разбора дня»/дайджеста (STYLE_CONTRACT п.3, п.6). Проверяем по подстроке
# (регистронезависимо), а не по точному заголовку — терпимо к пунктуации.
DAILY_SECTIONS = [
    ("что это значит для тебя", "нет секции «что это значит для тебя» (п.3)"),
    ("что делать", "нет вывода «что делать / чего не делать / что решать» (п.6)"),
]

MAX_LEN = 3500              # кэп артефакта (зеркалит bot_reports.DAILY_CASE_MAX)


# Легитимная типографика категории «So», которую НЕ считаем эмодзи: параграф, номер, градус, валюты,
# стрелка-логика каскада →. Всё остальное из «So»/эмодзи-плоскостей — декоративная пиктограмма (п.2).
_ALLOWED_SYMBOLS = set("§№°₽$€£→")


def _emoji_glyphs(text):
    """Список эмодзи/декоративных пиктограмм в тексте (нарушение п.2). Определяем по Unicode-блокам
    и категории Symbol-Other; легитимную типографику (§, №, °, валюты, → каскада) НЕ трогаем."""
    out = []
    for ch in text:
        if ch in _ALLOWED_SYMBOLS:
            continue
        cp = ord(ch)
        if (0x1F000 <= cp <= 0x1FAFF        # эмодзи-плоскости (иконки, лица, символы)
                or 0x2600 <= cp <= 0x27BF    # Misc symbols + Dingbats (☀✅⚠✂ и пр.)
                or 0x2190 <= cp <= 0x21FF    # стрелки (→ уже в allow-list; прочие — декор)
                or cp in (0xFE0F, 0x20E3)):  # variation selector / keycap
            out.append(ch)
        elif unicodedata.category(ch) == "So":   # прочие символы-пиктограммы
            out.append(ch)
    return out


def _unexplained_terms(text):
    """Термины стоп-списка, чьё ПЕРВОЕ вхождение не сопровождается расшифровкой в окне.
    Цитаты «…» (чужой текст: суд/новость) выключаются, как и у проверки тикеров —
    требовать расшифровку внутри дословной цитаты нельзя (П8: цитату не правим)."""
    text = _strip_quotes(text)
    low = text.lower()
    bad = []
    for term in STOPLIST:
        if term.isascii() and term.isalpha():
            m = re.search(r"\b" + re.escape(term) + r"\b", low)
        else:
            m = re.search(re.escape(term), low)
        if not m:
            continue
        # окно расшифровки = от термина до конца ТЕКУЩЕЙ фразы (. ! ? или перевод строки)
        rest = text[m.end():]
        end = re.search(r"[.!?\n]", rest)
        window = rest[: end.start()] if end else rest
        if not any(g in window for g in _GLOSS):
            bad.append(term)
    return bad


def lint_for_user(text, *, require_sections=None, max_len=None):
    """Общий проход: эмодзи + нерасшифрованный жаргон + (опц.) обязательные секции + (опц.) длина.
    require_sections — список (подстрока, сообщение). Возвращает список строк-нарушений."""
    viol = []
    if not text or not text.strip():
        return ["пустой текст артефакта"]
    gl = _emoji_glyphs(text)
    if gl:
        uniq = "".join(dict.fromkeys(gl))
        viol.append(f"эмодзи/пиктограммы запрещены (п.2): найдено {len(gl)} — {uniq}")
    for term in _unexplained_terms(text):
        viol.append(f"жаргон «{term}» без расшифровки при первом употреблении (п.2)")
    low = text.lower()
    for sub, msg in (require_sections or []):
        if sub.lower() not in low:
            viol.append(msg)
    if max_len and len(text) > max_len:
        viol.append(f"длина {len(text)} > лимита {max_len} знаков")
    return viol


# ── V0 «Идея дня» (карта v3, 31.10; формат владельца 15.07) ─────────────────────────────────────
# Правила подачи сверх общего прохода: ≤4 числа, 0 тех.id, тикеры только с именами, ≤3500 знаков.
# «Число» считаем ВНЕ цитат «…» (цитата = заголовок новости или реплика суда — их не редактируем) и
# без дат («5 ноября», 2026-11-05) — дата срока не «цифра анализа». Это зафиксировано здесь, а не в
# шаблоне, чтобы правило нельзя было обойти формулировкой.
IDEA_DAY_MAX_NUMBERS = 4
IDEA_DAY_MAX_LEN = 3500
_MONTHS_RU = ("январ", "феврал", "март", "апрел", "ма[яй]", "июн", "июл", "август", "сентябр",
              "октябр", "ноябр", "декабр")
_DATE_RE = re.compile(r"\b\d{1,2}\s+(?:" + "|".join(_MONTHS_RU) + r")\w*|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\.\d{2}\.\d{2,4}\b")
_NUM_RE = re.compile(r"(?<![A-Za-z§])[+−-]?\d+(?:[.,]\d+)?\s*%?")
_TICKER_RE = re.compile(r"\b[A-Z]{1,6}\.(?:US|INDX|LSE|TO)\b")
_TECH_ID_RE = re.compile(r"тех\.?\s*id|\b(?:ef|b4|calibrate|funnel)_\d{8}T\d{6}Z|\bwf_[0-9a-f]{6,}", re.IGNORECASE)
UNKNOWN_NAME_MARK = "компанию не опознал"


def _strip_quotes(text):
    """Текст без содержимого цитат «…» (новость/реплика суда — чужие слова, их числа не наши).

    Ре-гейт V0 02.11 (блокер fail-open): жадный «.*» съедал НАШ текст МЕЖДУ двумя цитатами
    на одной строке (similar_case_line несёт три пары) — числа/жаргон/тикеры там выпадали из
    блокирующего линта. Теперь пары режутся нежадно изнутри наружу: сначала самые внутренние
    «без-кавычек-внутри», повторно — пока есть пары (вложенные кавычки суда тоже схлопываются)."""
    out = []
    for line in text.split("\n"):
        prev = None
        while prev != line:
            prev = line
            # пара без кавычек внутри ЛИБО с уже схлопнутыми «» — так вложенность
            # сворачивается изнутри наружу, а текст между ОТДЕЛЬНЫМИ цитатами остаётся
            line = re.sub(r"«(?:[^«»]|«»)*»", "«»", line)
        out.append(line)
    return "\n".join(out)


_NAME_NUM_RE = re.compile(r"[^\s«»(]*\d[^\s«»(]*(?=\s+\([A-Z]{1,6}\.(?:US|INDX|LSE|TO)\))")


def count_numbers(text):
    """Числа анализа в тексте: вне цитат, без дат и без цифр в ИМЕНИ компании перед тикером
    («Phillips 66 (PSX.US)» — имя, не число анализа). Возвращает список найденных токенов."""
    t = _NAME_NUM_RE.sub(" ", _DATE_RE.sub(" ", _strip_quotes(text)))
    return [m.group(0).strip() for m in _NUM_RE.finditer(t)]


def _bare_tickers(text):
    """Тикеры без имени: тикер обязан стоять в скобках сразу после имени — «Nvidia (NVDA.US)» —
    либо нести честную пометку «компанию не опознал» (П8: имя не выдумываем)."""
    bad = []
    text = _strip_quotes(text)          # тикер внутри цитаты суда/новости — чужой текст, не наша подача
    for m in _TICKER_RE.finditer(text):
        before = text[max(0, m.start() - 2):m.start()]
        after = text[m.end():m.end() + 40]
        ok = before.endswith("(") or UNKNOWN_NAME_MARK in after
        if not ok:
            bad.append(m.group(0))
    return sorted(set(bad))


def lint_idea_of_day(text):
    """Блокирующий линт «Идеи дня» (V0): общий проход + ≤4 числа вне цитат + 0 тех.id + тикеры с
    именами + секции + длина. Непустой список = пуш НЕ уходит, алерт в дозор."""
    viol = lint_for_user(text, require_sections=DAILY_SECTIONS, max_len=IDEA_DAY_MAX_LEN)
    nums = count_numbers(text)
    if len(nums) > IDEA_DAY_MAX_NUMBERS:
        viol.append(f"чисел вне цитат {len(nums)} > {IDEA_DAY_MAX_NUMBERS}: {', '.join(nums[:8])}")
    if _TECH_ID_RE.search(text):
        viol.append("тех.id в тексте для владельца (V0: 0 тех.id)")
    bare = _bare_tickers(text)
    if bare:
        viol.append("тикеры без имени: " + ", ".join(bare))
    return viol


def check_daily_case(text):
    """Линтер «Разбора дня»: эмодзи + жаргон + обязательные секции + длина. Пустой список = чисто;
    непустой = сборка/пуш ВАЛИТСЯ (fail-closed)."""
    return lint_for_user(text, require_sections=DAILY_SECTIONS, max_len=MAX_LEN)


if __name__ == "__main__":       # ручной прогон: печатает нарушения из stdin
    import sys
    src = sys.stdin.read()
    v = check_daily_case(src)
    if v:
        print("НАРУШЕНИЯ СТИЛЬ-КОНТРАКТА:")
        for x in v:
            print("  -", x)
        sys.exit(1)
    print("чисто: артефакт проходит стиль-контракт")


def lint_week_odds(text):
    """Блокирующий линт «Шансов недели» (V5): секции/длина/жаргон/эмодзи как у дневного артефакта,
    0 тех.id, тикеры с именами. ЧИСЛА РАЗРЕШЕНЫ — проценты и пороги и есть содержание пуша."""
    viol = lint_for_user(text, require_sections=DAILY_SECTIONS, max_len=MAX_LEN)
    if _TECH_ID_RE.search(text):
        viol.append("тех.id в тексте для владельца")
    bare = _bare_tickers(text)
    if bare:
        viol.append("тикеры без имени: " + ", ".join(bare))
    return viol
