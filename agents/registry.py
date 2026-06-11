# -*- coding: utf-8 -*-
"""agents/registry.py — единый реестр агентов блоков B, C, D, G (MASTER_SPEC §4).

Единственный источник правды о составе агентов: id, блок, заголовок, какую РОЛЬ
модели (config/models.yaml) агент вызывает, «школа» ли это (выдвигает кандидатов в
поле суждений §5.2), и какой КОНТРАКТ ВЫВОДА он обязан соблюдать.

Используется и сборщиком промптов (agents/build_prompts.py), и оркестратором
(orchestrator/agents.py) — чтобы маршрутизация моделей и текст промптов не разъезжались.

Назначение моделей:
  • Явные роли §26 (поведенческий экономист, историк, своевременность, антиманипулятор,
    валидатор/reviewer, скоринг-контекст, риск) берутся из models.yaml.roles как есть.
  • §26 перечисляет АРХЕТИПЫ ролей, а не все 10 школ блока B поимённо. Недостающим
    школам/контролю модель назначается в models.yaml.school_roles — семьи РАЗНЕСЕНЫ
    (anthropic/openai/google/x-ai/deepseek/qwen) ради независимости голосов (§5.5:
    высокая корреляция оценок → ложная уверенность). Это инженерное заполнение пробела
    §26, помечено в models.yaml; состав/семьи меняются только с согласия пользователя.

Контракты вывода (output_kind) — все наследуют стандартное поле суждений Дирижёра §5.2
(вывод + вероятность + уверенность + данные-основания + что неизвестно):
  school_judgment   — школы B и прогнозные агенты C: + кандидаты[] (§6 этап 2)
  timing_verdict    — агент своевременности D: + вердикт РАНО/ВОВРЕМЯ/ПОЗДНО/ЛОВУШКА
  manipulation_score— антиманипулятор D: + балл 0–10, переворот
  control           — контроль G: процедурный вывод (блокировки, разрешимость, разбор)
"""

# (id, блок, заголовок §4, model_role, is_school, output_kind)
AGENTS = [
    # ── Блок B. Прогнозные школы ─────────────────────────────────────────────
    ("b_causal_links",          "B", "Агент жизненных взаимосвязей",        "causal_school",        True,  "school_judgment"),
    ("b_behavioral_economist",  "B", "Поведенческий экономист",             "behavioral_economist", True,  "school_judgment"),
    ("b_fundamental",           "B", "Фундаментальный аналитик",            "fundamental_school",   True,  "school_judgment"),
    ("b_technical",             "B", "Технический аналитик",                "technical_school",     True,  "school_judgment"),
    ("b_elliott_wave",          "B", "Волновик (Эллиотт)",                  "elliott_school",       True,  "school_judgment"),
    ("b_game_theory",           "B", "Теоретик игр",                        "game_theory_school",   True,  "school_judgment"),
    ("b_cyclist",               "B", "Циклист",                             "cyclist_school",       True,  "school_judgment"),
    ("b_omens",                 "B", "Агент примет (КАРАНТИН)",             "omens_school",         True,  "school_judgment"),
    ("b_historian_events",      "B", "Историк-1 «событие→следствия»",       "historian",            True,  "school_judgment"),
    ("b_historian_precursors",  "B", "Историк-2 «движение→предвестники»",   "historian",            True,  "school_judgment"),
    # ── Блок C. Каскады, неочевидность, контекст ─────────────────────────────
    ("c_cascades",              "C", "Агент каскадов",                      "cascade_school",       True,  "school_judgment"),
    ("c_adjacent_domains",      "C", "Агент смежных областей",              "adjacent_school",      True,  "school_judgment"),
    ("c_non_obviousness",       "C", "Оценщик неочевидности",               "non_obviousness",      False, "control"),
    ("c_context_filter",        "C", "Фильтр контекста",                    "context_scoring",      False, "control"),
    # ── Блок D. Тайминг и защита ─────────────────────────────────────────────
    ("d_timeliness",            "D", "Агент своевременности",               "timeliness",           False, "timing_verdict"),
    ("d_anti_manipulation",     "D", "Агент антиманипуляций",               "anti_manipulation",    False, "manipulation_score"),
    # ── Блок G. Обучение и контроль ──────────────────────────────────────────
    ("g_validator",             "G", "Валидатор (П8)",                      "data_reviewer",        False, "control"),
    ("g_predictions_journalist","G", "Журналист прогнозов (§9)",            "report_synthesizer",   False, "control"),
    ("g_outcome_analyst",       "G", "Разборщик результатов",               "outcome_analyst",      False, "control"),
    ("g_weight_calibrator",     "G", "Калибровщик весов",                   "context_scoring",      False, "control"),
    ("g_credibility",           "G", "Агент credibility",                   "data_reviewer",        False, "control"),
]

# Удобные представления
BY_ID = {a[0]: a for a in AGENTS}


def agents_in_block(block):
    return [a for a in AGENTS if a[1] == block]


def schools():
    """Школы — те, кто выдвигает кандидатов в поле суждений (§6 этап 2)."""
    return [a for a in AGENTS if a[4]]


def all_ids():
    return [a[0] for a in AGENTS]


def meta(agent_id):
    aid, block, title, role, is_school, kind = BY_ID[agent_id]
    return {
        "id": aid, "block": block, "title": title,
        "model_role": role, "is_school": is_school, "output_kind": kind,
    }
