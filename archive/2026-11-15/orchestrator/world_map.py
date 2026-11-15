# -*- coding: utf-8 -*-
"""orchestrator/world_map.py — Э4(а) «Перебор мира»: LLM-карта СЕГМЕНТОВ и МЕХАНИЗМОВ события.

Программа «Поисковый движок» (spec/ROADMAP_2026-07_search_engine.md, этап Э4, подписана 13.07):
вход — событие (новостной кластер / шок-источник), выход — структурированная карта сегментов
экономики с механизмами влияния (порядки 1–4, направление давления, канал переноса) — БЕЗ ТИКЕРОВ.

РАМКИ (нарушение = стоп этапа):
  • Рамка 2 программы: LLM предлагает ТОЛЬКО структуру (сегменты/механизмы/направления);
    ЧИСЛА НЕ ОЦЕНИВАЕТ — любое числовое поле в сегменте, кроме структурного «порядок» 1–4,
    отклоняется валидацией. Срок жизни карты (ttl_days) ставит КОД из config/limits.yaml.
  • Тикеры в карте ЗАПРЕЩЕНЫ: сегмент→инструменты превращает детерминированный скрин
    (orchestrator/segment_screen.py, Э4(б)), не LLM.
  • Бюджет: роль generator под НОВЫМ суб-потолком per_run_token_budget_usd.world_map
    (решение владельца 13.07 №8, инженерно) — гард ставит вызывающий (world_enum).
  • П8: мусорный/невалидный ответ → честный отказ с причиной, не «починка» ответа.
  • Провенанс карты (модель/ts/run_id/событие) — в протокол прогона.

Боевой контур event_first НЕ трогается (интеграция — этап Э5).
"""
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.segment_screen import EODHD_SECTORS   # noqa: E402  (единая точка правды секторов)

# Направления давления на сегмент — закрытый словарь (структура, не величина).
DIRECTIONS = ("рост", "снижение")


# Похоже на тикер (NUE, BNO.US, GEV) — карта обязана быть БЕЗ тикеров.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,4})?$")

# Тикеро-подобный токен ВНУТРИ свободного текста (Э4-ревью BLOCKER): $NUE (cashtag) или
# SYMBOL.EXCH (VRT.US, BNO.US). Латиница — кириллица «т.е.»/«и т.д.» сюда не попадает.
_TICKER_TOKEN_RE = re.compile(r"\$[A-Za-z]{1,6}\b|\b[A-Za-z]{1,6}\.[A-Z]{1,4}\b")
# Любая цифра в строковом поле карты = число (рамка 2: LLM величины НЕ оценивает — числа считает код).
_DIGIT_RE = re.compile(r"\d")


def _string_leak_problems(text, where):
    """Тикеро-подобные токены и числа в строковом поле карты (рамка 2). $X / X.US ловятся как
    тикеры; любая цифра — как запрещённая LLM-величина (проценты/цены/годы/амплитуды)."""
    problems = []
    if _TICKER_TOKEN_RE.search(text):
        problems.append(f"{where}: тикеро-подобный токен в тексте «{text[:48]}» — карта без тикеров (рамка 2)")
    if _DIGIT_RE.search(text):
        problems.append(f"{where}: число в тексте «{text[:48]}» — LLM-величины запрещены, числа считает код (рамка 2)")
    return problems


def _scan_leaks(obj, where):
    """РЕКУРСИВНАЯ проверка утечек: тикеры/числа в ЛЮБОЙ строке/числовом поле на любой глубине
    (Э4-ревью BLOCKER: раньше числа проверялись только на верхнем уровне сегмента, а тикеры —
    только в имени; канал/механизм/индустрии/событие/обоснование не сканировались). Ключ
    структурного «порядок» 1–4 — единственное разрешённое число, проверяется отдельно."""
    problems = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "порядок":
                continue                              # структурный порядок 1..4 — отдельная проверка
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                problems.append(f"{where}.{k}: числовое поле {v!r} — LLM-величины запрещены (рамка 2)")
            elif isinstance(v, str):
                problems += _string_leak_problems(v, f"{where}.{k}")
            else:
                problems += _scan_leaks(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                problems.append(f"{where}[{i}]: число {v!r} — LLM-величины запрещены (рамка 2)")
            elif isinstance(v, str):
                problems += _string_leak_problems(v, f"{where}[{i}]")
            else:
                problems += _scan_leaks(v, f"{where}[{i}]")
    return problems

WORLD_MAP_SYSTEM = (
    "Ты — картограф МИРА СОБЫТИЯ системы «Оракул» (этап Э4 «перебор мира»). По событию построй "
    "карту СЕГМЕНТОВ экономики, на которые событие давит, с МЕХАНИЗМАМИ влияния 1–4 порядка "
    "(П5: 1-й порядок отыгран HFT — обязательно ищи дальние порядки 2–4). "
    "СТРОГО ЗАПРЕЩЕНО: тикеры/названия компаний (инструменты подберёт детерминированный скрин) "
    "и ЛЮБЫЕ числовые оценки (вероятности, амплитуды, сроки) — числа считает код. "
    "НЕ выдумывай: если событие неясно или переноса на сегменты нет — верни пустой список "
    "сегментов и честно скажи почему (это поощряемый ответ). "
    "Поле «секторы» каждого сегмента — только из списка секторов EODHD: "
    + ", ".join(EODHD_SECTORS) + ". "
    "Верни РОВНО один объект JSON:\n"
    '{"событие":"...","сегменты":[{"сегмент":"...","порядок":2,"направление":"рост|снижение",'
    '"канал":"канал переноса","механизм":"почему событие давит на сегмент",'
    '"секторы":["Industrials"],"индустрии":["Electrical Equipment & Parts"]}],'
    '"обоснование":"...","уверенность":"низкая|средняя|высокая"}'
)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def map_ttl_days(limits=None):
    """Срок жизни карты — детерминированная константа КОДА (config/limits.yaml world_enum.map_ttl_days,
    решение №6/№8): LLM величины не оценивает (рамка 2). Нет ключа → консервативные 28 дней."""
    if limits is None:
        from mathlib import limits as L
        limits = L.load_limits()
    return int(((limits or {}).get("world_enum") or {}).get("map_ttl_days", 28))


def _segment_problems(seg, idx):
    """Список проблем ОДНОГО сегмента (пустой = валиден). П8: причины дословно."""
    problems = []
    if not isinstance(seg, dict):
        return [f"сегмент#{idx}: не объект"]
    name = seg.get("сегмент")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"сегмент#{idx}: пустое имя")
    elif _TICKER_RE.match(name.strip()):
        problems.append(f"сегмент#{idx}: имя '{name}' похоже на тикер — карта обязана быть БЕЗ тикеров")
    order = seg.get("порядок")
    if not isinstance(order, int) or isinstance(order, bool) or not (1 <= order <= 4):
        problems.append(f"сегмент#{idx}: порядок {order!r} вне 1..4")
    if seg.get("направление") not in DIRECTIONS:
        problems.append(f"сегмент#{idx}: направление {seg.get('направление')!r} вне {DIRECTIONS}")
    if not isinstance(seg.get("механизм"), str) or not seg.get("механизм", "").strip():
        problems.append(f"сегмент#{idx}: механизм пуст — пара без механизма не кандидат-ребро (Э4(ж))")
    sectors = seg.get("секторы")
    valid_sectors = [s for s in (sectors or []) if s in EODHD_SECTORS]
    if not valid_sectors:
        problems.append(f"сегмент#{idx}: ни одного валидного сектора EODHD в {sectors!r}")
    # тикеры запрещены в любом виде
    for k in ("тикеры", "tickers", "инструменты"):
        if k in seg:
            problems.append(f"сегмент#{idx}: поле {k!r} запрещено — инструменты подбирает скрин Э4(б)")
    # рамка 2 (РЕКУРСИВНО): числа и тикеро-подобные токены на любой глубине сегмента, включая
    # канал/механизм/индустрии/секторы — не только верхний уровень (Э4-ревью BLOCKER).
    problems += _scan_leaks(seg, f"сегмент#{idx}")
    return problems


def _normalize_segment(seg):
    """Оставить только допустимые ключи (структура). Вызывается ТОЛЬКО для валидных сегментов."""
    return {
        "сегмент": seg["сегмент"].strip(),
        "порядок": int(seg["порядок"]),
        "направление": seg["направление"],
        "канал": (seg.get("канал") or "").strip() or None,
        "механизм": seg["механизм"].strip(),
        "секторы": [s for s in (seg.get("секторы") or []) if s in EODHD_SECTORS],
        "индустрии": [str(i).strip() for i in (seg.get("индустрии") or [])
                      if isinstance(i, str) and i.strip()],
    }


def validate_map(doc):
    """Валидация формы ответа LLM. Возвращает (карта|None, problems).

    Сегмент с проблемами ОТБРАСЫВАЕТСЯ (с записью причины) — частично валидная карта живёт на
    валидных сегментах. Ни одного валидного сегмента ИЛИ сломан верхний уровень → карта None (П8)."""
    problems = []
    if not isinstance(doc, dict):
        return None, ["ответ не объект JSON"]
    segs = doc.get("сегменты")
    if not isinstance(segs, list):
        return None, ["поле «сегменты» отсутствует или не список"]
    # верхний уровень карты тоже сканируется на утечки (Э4-ревью BLOCKER: событие/обоснование —
    # свободный текст LLM, тикер/число там тоже нарушает рамку 2). Утечка верхнего уровня → отказ.
    top_leaks = []
    for k in ("событие", "обоснование"):
        v = doc.get(k)
        if isinstance(v, str):
            top_leaks += _string_leak_problems(v, k)
    if top_leaks:
        return None, top_leaks
    kept = []
    for i, seg in enumerate(segs):
        ps = _segment_problems(seg, i)
        if ps:
            problems.extend(ps)
        else:
            kept.append(_normalize_segment(seg))
    if not kept:
        problems.append("ни одного валидного сегмента")
        return None, problems
    карта = {
        "событие": str(doc.get("событие") or "").strip() or None,
        "сегменты": kept,
        "обоснование": str(doc.get("обоснование") or "").strip() or None,
        "уверенность": doc.get("уверенность")
        if doc.get("уверенность") in ("низкая", "средняя", "высокая") else None,
    }
    return карта, problems


def _parse_json(txt):
    txt = (txt or "").strip()
    start, end = txt.find("{"), txt.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(txt[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def build_world_map(event, client, *, run_id="world_map", limits=None):
    """Событие → карта мира через роль generator. Возвращает ЕДИНЫЙ конверт:

      {"карта": {...}|None, "отказ": None|причина, "проблемы_валидации": [...],
       "ttl_days": int (ставит КОД), "провенанс": {модель, ts, run_id, роль, cost_usd}}

    client — openrouter-клиент (MockClient в разработке/тестах; LIVE — только с этапа Э5).
    Бюджет-гард (RunBudgetGuard суб-потолка world_map) ставит вызывающий на client.cost_guard.
    П8: отказ LLM/мусор → честный отказ с причиной, прогон не роняем (fail-soft на уровне события).
    """
    user = ("Событие для карты мира:\n"
            f"- описание: {event.get('событие')}\n"
            f"- ключевые слова: {event.get('ключи')}\n"
            f"- источник шока (инструмент-исток): {event.get('источник_шока')}\n"
            f"- дата: {event.get('дата')}\n\n"
            "Построй карту сегментов по контракту из системного промпта. Тикеры и числа запрещены.")
    провенанс = {"ts": _now_iso(), "run_id": run_id, "роль": "generator",
                 "событие": event.get("событие"), "модель": None, "cost_usd": None}
    # Инв#5 / §24 (Э4-ревью medium): ПРЕД-проверка суб-потолка world_map ДО единственного LLM-вызова
    # (раньше стоял только стоп-на-лету RunBudgetGuard; пред-оценки не было). Отказ бюджета —
    # RunBudgetRefused, прогон карты не начинается. LIVE-путь Э5; на mock (cost 0) — no-op.
    from orchestrator import run_budget as RB
    RB.precheck_or_raise("world_map")     # реальные потолки production (Инв#5), не тест-инъекция
    try:
        res = client.complete("generator", WORLD_MAP_SYSTEM, user,
                              agent_id="world_mapper", output_kind="world_map")
    except Exception as e:  # noqa: BLE001 — RunBudgetExceeded (BaseException) НЕ ловится, стоп легитимен (§24)
        return {"карта": None, "отказ": f"сбой LLM-картографа мира: {type(e).__name__}: {e}",
                "проблемы_валидации": [], "ttl_days": map_ttl_days(limits), "провенанс": провенанс}
    провенанс["модель"] = res.get("model")
    провенанс["cost_usd"] = res.get("cost")
    doc = _parse_json(res.get("text"))
    if doc is None:
        return {"карта": None, "отказ": "ответ LLM не парсится как JSON (П8: не чиним, отказ)",
                "проблемы_валидации": [], "ttl_days": map_ttl_days(limits), "провенанс": провенанс}
    if isinstance(doc.get("сегменты"), list) and not doc["сегменты"]:
        # честное «переноса нет» — поощряемый ответ (П8), это отказ ПО СОБЫТИЮ, не мусор
        return {"карта": None,
                "отказ": "карта пуста: " + (str(doc.get("обоснование") or "перенос на сегменты не найден")),
                "проблемы_валидации": [], "ttl_days": map_ttl_days(limits), "провенанс": провенанс}
    карта, problems = validate_map(doc)
    if карта is None:
        return {"карта": None, "отказ": "форма ответа невалидна: " + "; ".join(problems[:6]),
                "проблемы_валидации": problems, "ttl_days": map_ttl_days(limits), "провенанс": провенанс}
    return {"карта": карта, "отказ": None, "проблемы_валидации": problems,
            "ttl_days": map_ttl_days(limits), "провенанс": провенанс}
