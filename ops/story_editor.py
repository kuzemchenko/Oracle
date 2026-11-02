# -*- coding: utf-8 -*-
"""ops/story_editor.py — РЕДАКТОР-РАССКАЗЧИК подачи (вердикт владельца 02.11:
«я в Телеграме вообще ничего не понимаю» = формат НЕ тот).

Диагноз: детерминированный рендер склеивает куски машинного протокола (узлы картографа,
цитаты суда, «неотыгранный остаток») — честно, но нечитаемо для цели продукта
(«девушка с экономическим образованием и общим пониманием мира»). Линт ловит механику,
а не понятность.

Контур: готовый строгий текст «Идеи дня» → LLM-редактор переписывает его РАССКАЗОМ
(что случилось → кто и почему на этом зарабатывает/теряет → похожий случай → сколько ×
шанс × срок → кто на другой стороне → главный скепсис → что делать). Редактор НЕ аналитик
и НЕ источник фактов — только язык.

Гарды КОДОМ (П8, Инв#6 — не доверяем редактору на слово):
  • каждое ЧИСЛО рассказа обязано присутствовать во входе (токены нормализованы:
    запятая=точка, ведущие нули срезаны) — добавил своё число → ОТКАЗ;
  • каждый ТИКЕР рассказа — из входа; добавил бумагу → ОТКАЗ;
  • блокирующий линт подачи (lint_idea_of_day: секции, ≤4 числа вне цитат, 0 тех.id,
    тикеры с именами, длина) — не прошёл → ОТКАЗ;
  • рамка «исследовательский инструмент, не инвестиционная рекомендация» обязана остаться.
ОТКАЗ → бот шлёт строгий формат + заметка в дозор (пуш не теряется, fail-soft).

ПРАВИЛО ВНЕДРЕНИЯ (01.11): за флагом story_editor=false в config/presentation.yaml —
включается после ручного прогона испытателя и «ок» владельца.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROLE = "story_editor"
PROMPT = ROOT / "agents" / "prompts" / "story_editor.md"
MAX_LEN = 3500

# Ре-гейт №2 02.11: (1) знак — часть числа («−0.4» ≠ «0.4»: переворот знака матожидания —
# выдуманное число); (2) тикеры ловятся И в голой форме — промпт v2 велит писать тикер БЕЗ
# «.US», прежний регексп только суффиксной формы делал гард вакуумным; суффиксы — полный
# список контракта подачи (US|INDX|LSE|TO), не только .US.
_NUM_RE = re.compile(r"[+\-−]?\d+(?:[.,]\d+)?")
_TICKER_SUF_RE = re.compile(r"\b([A-Z][A-Z0-9]{0,5})\.(?:US|INDX|LSE|TO)\b")
_BARE_TICKER_RE = re.compile(r"\b[A-Z]{2,6}\b")
# латинские капс-слова, которые в наших текстах НЕ тикеры (не браковать честный рассказ)
_NOT_TICKERS = {"AI", "CEO", "CPI", "ETF", "EV", "GDP", "IPO", "IT", "LNG", "OPEC",
                "PMI", "RSI", "US", "USA"}
FRAME = "не инвестиционная рекомендация"


def _norm_num(tok):
    t = tok.replace("−", "-").replace(",", ".")
    sign = ""
    if t and t[0] in "+-":
        sign, t = ("" if t[0] == "+" else "-"), t[1:]
    t = t.lstrip("0") or "0"
    if t.startswith("."):
        t = "0" + t
    if "." in t:
        t = t.rstrip("0").rstrip(".") or "0"
    if t == "0":
        sign = ""
    return sign + t


def _nums(text):
    t = str(text or "")
    # Перепроверка №2 02.11: «минус 0.4» / «плюс 0.4» прописью → знаковый токен (иначе
    # честный словесный знак бракуется, а молчаливый переворот — нет)
    t = re.sub(r"\bминус\s+(?=\d)", "−", t)
    t = re.sub(r"\bплюс\s+(?=\d)", "+", t)
    out = set()
    for m in _NUM_RE.finditer(t):
        tok = m.group(0)
        # дефис после буквы/цифры — разделитель диапазона/кода («3-5», «Q4-23»,
        # «2026-11-10»), не знак минус (как в mechanism_match._obs_sign)
        if tok[0] in "+-−" and m.start() > 0 and t[m.start() - 1].isalnum():
            tok = tok[1:]
        out.add(_norm_num(tok))
    return out


def _tickers(text):
    s = str(text or "")
    return set(_TICKER_SUF_RE.findall(s)) | (set(_BARE_TICKER_RE.findall(s)) - _NOT_TICKERS)


def verify(story, source):
    """Детерминированная проверка рассказа против строгого входа. → список нарушений."""
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    viol = []
    s = str(story or "").strip()
    if not s:
        return ["пустой рассказ"]
    extra_n = _nums(s) - _nums(source)
    if extra_n:
        viol.append("числа не из входа (П8): " + ", ".join(sorted(extra_n)[:5]))
    extra_t = _tickers(s) - _tickers(source)
    if extra_t:
        viol.append("тикеры не из входа (П8): " + ", ".join(sorted(extra_t)[:5]))
    # Перепроверка №2 02.11: числа ПРОПИСЬЮ («в девяти случаях из десяти», «втрое больше»)
    # обходили цифровой гард — количественные слова рассказа обязаны быть и во входе
    extra_q = PL.quantity_markers(s) - PL.quantity_markers(source)
    if extra_q:
        viol.append("количественные слова не из входа (П8): " + ", ".join(sorted(extra_q)[:4]))
    if FRAME not in s:
        viol.append("потеряна рамка «исследовательский инструмент»")
    if len(s) > MAX_LEN:
        viol.append(f"длина {len(s)} > {MAX_LEN}")
    viol += PL.lint_idea_of_day(s)
    return viol


def rewrite(source_text, *, client=None, mode="auto", run_id=None):
    """Строгий текст → рассказ. Возвращает (story|None, причина_отказа|None).
    Любой сбой/нарушение → (None, причина): бот уходит на строгий формат (fail-soft)."""
    src = str(source_text or "").strip()
    if not src:
        return None, "пустой вход"
    try:
        system = PROMPT.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"нет промпта редактора: {e}"
    try:
        if client is None:
            from orchestrator.openrouter import make_client
            client = make_client(mode=mode, run_id=run_id or "story_editor")
        # Ре-гейт V0 02.11 (БЛОКЕР Инв#5/§24): live-вызов редактора — только через бюджетный
        # контур (пред-проверка + суб-потолок story_editor + стоп-на-лету).
        from orchestrator import run_budget as RB
        if getattr(client, "mode", None) == "live":
            decision = RB.precheck("story_editor")
            if not decision.get("allowed"):
                return None, f"бюджет §24: {decision.get('reason') or decision}"
            client.cost_guard = RB.RunBudgetGuard("story_editor", decision["cap_usd"])
        try:
            resp = client.complete(ROLE, system, src, agent_id="story_editor",
                                   output_kind="story_rewrite")
        except RB.RunBudgetExceeded as e:        # BaseException — отдельная ветка (Инв#5)
            return None, f"ОСТАНОВ бюджета §24: {e.spent_usd:.4f} при потолке {e.cap_usd}"
        story = (resp.get("text") if isinstance(resp, dict) else resp) or ""
    except Exception as e:                                   # noqa: BLE001 — подача не падает
        return None, f"редактор недоступен: {type(e).__name__}: {e}"
    story = story.strip()
    viol = verify(story, src)
    if viol:
        return None, "; ".join(viol[:3])
    return story, None
