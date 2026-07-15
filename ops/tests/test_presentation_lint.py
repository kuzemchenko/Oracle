# -*- coding: utf-8 -*-
"""Тесты линтера стиль-контракта (ops/presentation_lint) + рендера «Разбора дня» (bot_reports).

Держит §6 STYLE_CONTRACT: ноль эмодзи, обязательные секции, расшифровка жаргона при первом
употреблении. Ключевой тест: линтер ВАЛИТ сломанный шаблон (иначе он бесполезен)."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import presentation_lint as PL          # noqa: E402
import bot_reports as BR                # noqa: E402
from orchestrator import daily_case as DC   # noqa: E402


def _live_case():
    proto = {"run_id": "ef_T", "ts": "2026-07-15T09:00:00Z",
             "граф_отбор": {"топ_k": [{
                 "актив": "GEV.US", "событие": "Бум ИИ-датацентров",
                 "якорь": "VRT.US", "порядок": 3, "edge": 0.08, "надёжность_r2": 0.34,
                 "чокпоинт": True, "отыгранность_узла": 0.25,
                 "продуктовый_ранг": {"компоненты": {"неочевидность": 0.66}},
                 "узлы_каскада": [{"порядок": 3, "узел": "дефицит трансформаторов",
                                   "чокпоинт": True, "тикеры": ["GEV.US"]}]}],
                 "суд_money": {"GEV.US": {"исход": "УСТОЯЛА", "балл": 3.4, "порог": 3.0,
                                          "кто_продаёт_нам": "скептик: уже в цене"}}}}
    return DC.select_case(proto, name_fn=lambda s: {"GEV.US": "GE Vernova"}.get(s, s))


# ── линтер ────────────────────────────────────────────────────────────────────────
def test_emoji_detected():
    v = PL.lint_for_user("Текст с иконкой 🧭 внутри")
    assert any("эмодзи" in x for x in v)


def test_clean_text_passes():
    assert PL.lint_for_user("Обычный деловой текст без иконок — с тире и цифрами 25%.") == []


def test_unexplained_jargon_flagged():
    v = PL.lint_for_user("Сигнал прошёл FDR и попал в tier A.")
    assert any("FDR" in x or "fdr" in x for x in v)
    assert any("tier" in x for x in v)


def test_explained_jargon_passes():
    v = PL.lint_for_user("Прошёл контроль ложных открытий (FDR — проверка на случайность).")
    assert v == []


def test_required_sections_missing_flagged():
    v = PL.lint_for_user("Заголовок без нужных секций.",
                         require_sections=PL.DAILY_SECTIONS)
    assert len(v) == 2                       # нет «что это значит», нет «что делать»


def test_broken_daily_template_fails():
    """Главный тест: сломанный шаблон (эмодзи + нет «что делать» + голый жаргон) обязан ВАЛИТЬСЯ."""
    broken = ("Разбор дня 🔴\nЧто это значит для тебя. Ерунда.\nТезис. Важный каскад.")
    v = PL.check_daily_case(broken)
    assert any("эмодзи" in x for x in v)
    assert any("каскад" in x for x in v)
    assert any("что делать" in x for x in v)


# ── рендер «Разбора дня» ───────────────────────────────────────────────────────────
def test_daily_case_render_passes_linter():
    text = BR.format_daily_case(_live_case())
    assert PL.check_daily_case(text) == [], PL.check_daily_case(text)


def test_daily_case_render_no_emoji_and_has_sections():
    text = BR.format_daily_case(_live_case())
    assert "Что это значит для тебя." in text
    assert "Что делать." in text
    assert "Твой ход:" in text
    assert len(text) <= BR.DAILY_CASE_MAX


def test_empty_case_render_is_honest():
    text = BR.format_daily_case({"пусто": "нет материала", "тех_id": "ef_X", "дата": "15 июля"})
    assert PL.check_daily_case(text) == []       # даже пустой день проходит контракт
    assert "не набралось" in text
