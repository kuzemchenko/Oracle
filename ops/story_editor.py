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

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,5}\.US\b")
FRAME = "не инвестиционная рекомендация"


def _norm_num(tok):
    t = tok.replace(",", ".")
    t = t.lstrip("0") or "0"
    if t.startswith("."):
        t = "0" + t
    if "." in t:
        t = t.rstrip("0").rstrip(".") or "0"
    return t


def _nums(text):
    return {_norm_num(t) for t in _NUM_RE.findall(str(text or ""))}


def _tickers(text):
    return set(_TICKER_RE.findall(str(text or "")))


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
        resp = client.complete(ROLE, system, src, agent_id="story_editor",
                               output_kind="story_rewrite")
        story = (resp.get("text") if isinstance(resp, dict) else resp) or ""
    except Exception as e:                                   # noqa: BLE001 — подача не падает
        return None, f"редактор недоступен: {type(e).__name__}: {e}"
    story = story.strip()
    viol = verify(story, src)
    if viol:
        return None, "; ".join(viol[:3])
    return story, None
