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
STOPLIST = ["q-value", "fdr", "z-score", "tier", "ярус", "каскад", "brier", "σ", "r²"]

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
    """Термины стоп-списка, чьё ПЕРВОЕ вхождение не сопровождается расшифровкой в окне."""
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
