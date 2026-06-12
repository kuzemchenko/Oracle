# -*- coding: utf-8 -*-
"""orchestrator/judgment.py — стандартное поле суждений Дирижёра (§5.2) и валидация П8.

Поле суждений в стандартном формате (§5.2): вывод + вероятность + уверенность +
данные-основания + что неизвестно. Школы добавляют кандидатов (§6 этап 2); тайминг —
вердикт; антиманипулятор — балл; контроль G — процедурный вердикт.

Здесь — НЕ LLM: детерминированный разбор/валидация JSON-ответа агента и программная
проверка инварианта П8 (вероятность без оснований → ВОЗВРАТ). Это «валидатор в коде»,
дополняющий агента-валидатора G (двойной контур: модель + код).
"""
import json
import re

CONFIDENCE = {"низкая", "средняя", "высокая"}
DIRECTIONS = {"лонг", "шорт"}
TIMING_VERDICTS = {"РАНО", "ВОВРЕМЯ", "ПОЗДНО", "ЛОВУШКА"}
JUDGE_VERDICTS = {"УСТОЯЛА", "РАЗБИТА"}
NO_DATA = "нет данных"

# Ключевые поля стандартного поля суждений (§5.2), общие для всех видов.
BASE_FIELDS = ("вывод", "вероятность", "уверенность", "данные_основания", "что_неизвестно")


class JudgmentError(ValueError):
    """Ответ агента не парсится/не валиден как поле суждений."""


def extract_json(text):
    """Достаёт ОДИН объект JSON из ответа модели.

    Терпим к обёрткам ```json ... ``` и к ведущему/хвостовому тексту: берём подстроку
    от первой '{' до парной ей '}'. Если объекта нет — JudgmentError (а не выдумка).
    """
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str) or not text.strip():
        raise JudgmentError("пустой ответ модели")
    # снять markdown-ограждение, если есть
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    body = fence.group(1) if fence else text
    start = body.find("{")
    if start < 0:
        raise JudgmentError("в ответе нет JSON-объекта")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(body)):
        ch = body[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = body[start:i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError as e:
                    raise JudgmentError(f"JSON не парсится: {e}")
    raise JudgmentError("незакрытый JSON-объект в ответе")


def _is_no_data(obj):
    v = str(obj.get("вывод", "")).strip().lower()
    return v.startswith(NO_DATA)


def _norm_prob(p):
    if p is None:
        return None
    if isinstance(p, bool):
        raise JudgmentError("вероятность не должна быть bool")
    if not isinstance(p, (int, float)):
        raise JudgmentError(f"вероятность не число: {p!r}")
    if not (0.0 <= float(p) <= 1.0):
        raise JudgmentError(f"вероятность вне [0,1]: {p}")
    return float(p)


def parse(raw_text, output_kind):
    """Разбирает ответ агента в нормализованный dict поля суждений.

    Бросает JudgmentError при структурных проблемах. НЕ применяет П8-ворота (это validate()):
    парсинг отделён от валидации, чтобы протокол мог записать и невалидное суждение с пометкой.
    """
    obj = extract_json(raw_text)
    if not isinstance(obj, dict):
        raise JudgmentError("корень ответа — не объект")

    missing = [f for f in BASE_FIELDS if f not in obj]
    if missing:
        raise JudgmentError(f"нет обязательных полей §5.2: {missing}")

    obj["вероятность"] = _norm_prob(obj.get("вероятность"))
    if "base_rate" in obj:
        obj["base_rate"] = _norm_prob(obj.get("base_rate"))

    conf = str(obj.get("уверенность", "")).strip().lower()
    if conf not in CONFIDENCE:
        raise JudgmentError(f"уверенность не из {CONFIDENCE}: {obj.get('уверенность')!r}")
    obj["уверенность"] = conf

    if not isinstance(obj.get("данные_основания"), list):
        raise JudgmentError("данные_основания должны быть списком")
    if not isinstance(obj.get("что_неизвестно"), list):
        raise JudgmentError("что_неизвестно должно быть списком")

    if output_kind == "school_judgment":
        cands = obj.setdefault("кандидаты", [])
        if not isinstance(cands, list):
            raise JudgmentError("кандидаты должны быть списком")
        for c in cands:
            d = str(c.get("направление", "")).strip().lower()
            if d and d not in DIRECTIONS:
                raise JudgmentError(f"направление кандидата не из {DIRECTIONS}: {d!r}")
    elif output_kind == "timing_verdict":
        v = str(obj.get("вердикт", "")).strip().upper()
        if v not in TIMING_VERDICTS:
            raise JudgmentError(f"вердикт тайминга не из {TIMING_VERDICTS}: {obj.get('вердикт')!r}")
        obj["вердикт"] = v
    elif output_kind == "manipulation_score":
        b = obj.get("балл")
        if not isinstance(b, (int, float)) or isinstance(b, bool) or not (0 <= b <= 10):
            raise JudgmentError(f"манипуляционный балл вне 0..10: {b!r}")
    elif output_kind == "control":
        # вероятность контроля всегда null (процедурный голос, §5.6)
        if obj.get("вероятность") is not None:
            raise JudgmentError("у контрольного агента вероятность должна быть null (§5.6)")
    elif output_kind == "judge_verdict":
        v = str(obj.get("вердикт", "")).strip().upper()
        if v not in JUDGE_VERDICTS:
            raise JudgmentError(f"вердикт судьи не из {JUDGE_VERDICTS}: {obj.get('вердикт')!r}")
        obj["вердикт"] = v
        if not isinstance(obj.get("рубрика"), dict):
            raise JudgmentError("судья обязан вернуть рубрику с оценками (config/rubric.yaml)")
        # два обязательных вопроса §4 блок E (процедурное вето §5.6 при отсутствии — в debate.py)
    elif output_kind == "risk_assessment":
        # риск-агент НЕ голосует о направлении рынка (§4 блок F): вероятность всегда null
        if obj.get("вероятность") is not None:
            raise JudgmentError("у риск-агента вероятность должна быть null (§4: не голос о рынке)")
        if not isinstance(obj.get("сценарии", []), list):
            raise JudgmentError("сценарии риск-агента должны быть списком")
    elif output_kind == "report":
        if not isinstance(obj.get("поля"), dict):
            raise JudgmentError("отчёт §8 обязан содержать объект 'поля' с 13 полями")

    obj["_output_kind"] = output_kind
    obj["_no_data"] = _is_no_data(obj)
    return obj


def validate_p8(obj):
    """Программная П8-ворота. Возвращает список нарушений (пустой = чисто).

    Правило §5.2/П8: вероятность без хотя бы одного основания → возврат. «Нет данных»
    (вероятность=null, вывод='нет данных') — легитимно и НЕ нарушение.
    """
    violations = []
    prob = obj.get("вероятность")
    grounds = obj.get("данные_основания") or []
    if prob is not None and len(grounds) == 0:
        violations.append("вероятность задана, но данные_основания пусты (П8: вероятность без оснований)")
    # каждое основание должно иметь источник (иначе это утверждение без ссылки)
    for i, g in enumerate(grounds):
        if not isinstance(g, dict) or not str(g.get("источник", "")).strip():
            violations.append(f"основание #{i} без поля 'источник' (П8: факт без ссылки)")
    # школа с кандидатами, но без оснований — выдумка идеи
    if obj.get("кандидаты") and len(grounds) == 0 and not obj.get("_no_data"):
        violations.append("есть кандидаты, но нет ни одного основания (П8)")
    return violations


def is_clean(obj):
    return not validate_p8(obj)
