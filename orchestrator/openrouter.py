# -*- coding: utf-8 -*-
"""orchestrator/openrouter.py — вызовы моделей через OpenRouter по config/models.yaml.

Инварианты:
  • П10: семьи Генератор/Критик/Судья различны — здесь живёт helper judge_family_ok()
    (правило fallback_policy: фолбек судьи не из семейства генератора текущей идеи).
  • §26 фолбеки: автопереход при unavailable/timeout/refusal/rate_limit; КАЖДАЯ смена
    модели пишется в протокол прогона journal/funnel_logs/.
  • Бюджет (§30, limits.yaml): каждый вызов пишет строку в journal/costs.jsonl
    (ts, mode, agent, model, tokens, cost). Стоимость берётся из ответа OpenRouter;
    если провайдер её не вернул — пишем null (П8: не выдумываем цену).

Два клиента с общим интерфейсом complete(role, system, user, *, agent_id, output_kind):
  LiveClient — реальные HTTP-вызовы OpenRouter (ключ OPENROUTER_API_KEY).
  MockClient — детерминированные синтетические суждения БЕЗ сети и БЕЗ трат (для
    сквозного дымового теста воронки — гейт Недели 5–6 не должен жечь бюджет).
"""
import os
import json
import time
import hashlib
import pathlib
import datetime

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODELS_YAML = ROOT / "config" / "models.yaml"
COSTS_LOG = ROOT / "journal" / "costs.jsonl"
FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_models(path=MODELS_YAML):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_role(role, models=None):
    """Возвращает конфиг роли: {primary, fallbacks, family, temperature, reasoning}.

    Ищет роль и в roles (§26), и в school_roles (заполнение пробела §26). Неизвестная
    роль — KeyError (не молчим, не выдумываем модель).
    """
    models = models or load_models()
    for section in ("roles", "school_roles"):
        block = models.get(section, {})
        if role in block:
            r = dict(block[role])
            r.setdefault("fallbacks", [])
            r.setdefault("temperature", 0.7)
            r.setdefault("reasoning", False)
            return r
    raise KeyError(f"роль '{role}' не найдена ни в roles, ни в school_roles models.yaml")


def family_of(model_id, models=None):
    """Семейство модели по префиксу провайдера OpenRouter (anthropic/openai/google/...)."""
    return model_id.split("/", 1)[0] if "/" in model_id else model_id


def judge_family_ok(judge_model, generator_family, models=None):
    """П10: фолбек судьи не из семейства генератора текущей идеи (fallback_policy)."""
    return family_of(judge_model, models) != generator_family


def _model_chain(role_cfg):
    return [role_cfg["primary"]] + list(role_cfg.get("fallbacks", []))


def _exclude_set(exclude_family):
    """Нормализует exclude_family к множеству семейств. Принимает str (одно семейство,
    обратная совместимость) ИЛИ итерируемое семейств (F3#23: развязка судьи со ВСЕЙ
    цепочкой дебатёров — генератор+критик+адвокат, не только генератор)."""
    if not exclude_family:
        return set()
    if isinstance(exclude_family, str):
        return {exclude_family}
    return {f for f in exclude_family if f}


def filtered_chain(role_cfg, exclude_family=None, models=None):
    """Цепочка моделей роли с ИСКЛЮЧЕНИЕМ семейства(-в) (П10).

    exclude_family — семейство (str) ИЛИ набор семейств (F3#23). Для судьи: ни primary, ни
    один фолбек не должен быть из семейств дебатёров текущей идеи (генератор/критик/адвокат —
    судья слепо оценивает их аргументы, значит должен быть НЕЗАВИСИМ от каждого). Для критика:
    не из семейства генератора (адверсария). Возвращает отфильтрованную цепочку; пустая →
    RuntimeError (не молчим — лучше явный отказ, чем тихое нарушение П10)."""
    chain = _model_chain(role_cfg)
    excl = _exclude_set(exclude_family)
    if not excl:
        return chain
    kept = [m for m in chain if family_of(m, models) not in excl]
    if not kept:
        raise RuntimeError(
            f"П10: вся цепочка роли из исключённых семейств {sorted(excl)} — "
            f"слепой суд/развязка невозможны; нужна модель другого семейства (config/models.yaml)")
    return kept


# ── Логирование ─────────────────────────────────────────────────────────────────
_LOG_LOCK = __import__("threading").Lock()  # сериализует дозапись журналов при параллельных кейсах


def log_cost(mode, agent_id, model, usage, cost, ok=True, run_id=None):
    """Строка в journal/costs.jsonl (limits.yaml: costs_log). Append-only, потокобезопасно."""
    COSTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _now(), "mode": mode, "agent": agent_id, "model": model,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "cost_usd": cost, "ok": ok, "run_id": run_id,
    }
    with _LOG_LOCK, open(COSTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def log_model_swap(run_id, agent_id, role, attempted, used, reason):
    """Смена модели (фолбек) → протокол прогона (§26: важно, кто судил)."""
    FUNNEL_LOGS.mkdir(parents=True, exist_ok=True)
    path = FUNNEL_LOGS / f"{run_id}_model_swaps.jsonl"
    rec = {"ts": _now(), "agent": agent_id, "role": role,
           "attempted": attempted, "used": used, "reason": reason}
    with _LOG_LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Базовый клиент ──────────────────────────────────────────────────────────────
class BaseClient:
    mode = "base"

    def __init__(self, models=None, run_id=None):
        self.models = models or load_models()
        self.run_id = run_id or "run"
        self.cost_guard = None  # RunBudgetGuard: стоп прогона на лету (§24), ставится оркестратором

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        raise NotImplementedError


# ── Реальный клиент OpenRouter ──────────────────────────────────────────────────
class LiveClient(BaseClient):
    mode = "live"

    def __init__(self, models=None, run_id=None, mode="live", timeout=90, api_key=None):
        super().__init__(models, run_id)
        self.mode = mode
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY не задан — LiveClient невозможен (используйте MockClient)")
        import requests  # ленивый импорт: mock-путь не требует requests
        self._requests = requests

    def _one_call(self, model, role_cfg, system, user):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/oracle-local",
            "X-Title": "Oracle funnel",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": role_cfg.get("temperature", 0.7),
            "usage": {"include": True},  # просим OpenRouter вернуть стоимость генерации
        }
        resp = self._requests.post(OPENROUTER_URL, headers=headers,
                                   json=body, timeout=self.timeout)
        if resp.status_code == 429:
            raise _Retryable("rate_limit", f"429 {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise _Retryable("unavailable", f"{resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise _Retryable("refusal", f"нет choices: {str(data)[:200]}")
        text = choices[0].get("message", {}).get("content") or ""
        if not str(text).strip():
            # пустой/None контент — транзиент: фолбек на следующую модель цепочки (§26), а не падение
            raise _Retryable("empty", f"пустой content от {model}: {str(data)[:160]}")
        usage = data.get("usage") or {}
        cost = usage.get("cost")  # OpenRouter возвращает реальную стоимость при usage.include
        return text, usage, cost

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        role_cfg = resolve_role(role, self.models)
        chain = filtered_chain(role_cfg, exclude_family, self.models)  # П10: фильтр семейства
        last_err = None
        for i, model in enumerate(chain):
            try:
                text, usage, cost = self._one_call(model, role_cfg, system, user)
                if i > 0:
                    log_model_swap(self.run_id, agent_id, role, chain[0], model,
                                   reason=str(last_err))
                log_cost(self.mode, agent_id, model, usage, cost, ok=True, run_id=self.run_id)
                if self.cost_guard is not None:  # стоп прогона на лету (§24): рвёт при пересечении потолка
                    self.cost_guard.add(cost)
                return {"text": text, "model": model, "usage": usage,
                        "cost": cost, "fallback_index": i}
            except _Retryable as e:
                last_err = f"{e.kind}: {e}"
                log_cost(self.mode, agent_id, model, None, None, ok=False, run_id=self.run_id)
                continue
            except Exception as e:  # сетевые/прочие — тоже триггер фолбека (§26)
                last_err = f"error: {type(e).__name__}: {e}"
                log_cost(self.mode, agent_id, model, None, None, ok=False, run_id=self.run_id)
                continue
        raise RuntimeError(f"все модели роли '{role}' исчерпаны для {agent_id}: {last_err}")


class _Retryable(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


# ── Mock-клиент: детерминированные суждения без сети и трат ──────────────────────
class MockClient(BaseClient):
    """Синтезирует ВАЛИДНЫЕ по формату §5.2 суждения детерминированно из (agent_id, срез).

    Нужен для сквозного дымового теста воронки (гейт Недели 5–6) — он не должен жечь
    бюджет и зависеть от сети. Mock НЕ моделирует качество рассуждений (это проверяет
    маскированный смоук §23.2б Недели 8) — только формат и поток данных воронки.
    """
    mode = "mock"

    def __init__(self, models=None, run_id=None, abstain_every=4):
        super().__init__(models, run_id)
        # каждый N-й агент (по детерм. хешу) воздерживается — проверяем «нет данных»-путь
        self.abstain_every = abstain_every

    def _h(self, *parts):
        raw = "|".join(str(p) for p in parts)
        return int(hashlib.sha256(raw.encode("utf-8")).hexdigest(), 16)

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        role_cfg = resolve_role(role, self.models)
        model = filtered_chain(role_cfg, exclude_family, self.models)[0]  # П10: фильтр семейства
        payload = self._synthesize(agent_id, output_kind, user)
        text = json.dumps(payload, ensure_ascii=False)
        # mock НЕ пишет в journal/costs.jsonl: это не трата, бюджет (§30) считает только live.
        return {"text": text, "model": model, "usage": {}, "cost": 0.0, "fallback_index": 0}

    # типы вывода блоков E/F работают на КОНКРЕТНОЙ идее (этапы 5–6) — в mock они не
    # «воздерживаются» по хешу (иначе конвейер дебатов рвётся на пустом месте); воздержание
    # моделируем только для широкого поля суждений (B/C/D/G).
    _DEBATE_KINDS = {"generator_hypothesis", "critique", "advocacy", "judge_verdict",
                     "risk_assessment", "report"}

    def _synthesize(self, agent_id, output_kind, user):
        h = self._h(agent_id, output_kind, len(user))
        abstain = (h % self.abstain_every == 0) and output_kind not in self._DEBATE_KINDS

        # источники берём из реального среза (user-промпт), чтобы П8 проходил честно:
        # ищем тикеры универсума, упомянутые в срезе. Чтобы разные школы выдвигали РАЗНЫЕ
        # активы (а не все один BNO.US) — выбираем среди присутствующих детерминированно по хешу.
        ground_src = "context:market_slice"
        present = [s for s in ("BNO.US", "USO.US", "SPY.US", "DBC.US", "CPER.US", "COPX.US") if s in user]
        sym = present[self._h(agent_id) % len(present)] if present else None

        if output_kind in self._DEBATE_KINDS:
            # направление берём из поданного дела (а не из хеша) — когерентность с реальной идеей
            forced_dir = None
            if '"направление": "шорт"' in user or '"направление":"шорт"' in user:
                forced_dir = "шорт"
            elif '"направление": "лонг"' in user or '"направление":"лонг"' in user:
                forced_dir = "лонг"
            return self._synthesize_debate(agent_id, output_kind, sym, h, ground_src, forced_dir)

        if abstain or sym is None:
            base = {
                "вывод": "нет данных",
                "вероятность": None,
                "base_rate": None,
                "уверенность": "низкая",
                "данные_основания": [],
                "что_неизвестно": [f"{agent_id}: поданный срез недостаточен для вывода (mock-воздержание)"],
            }
            if output_kind == "school_judgment":
                base["кандидаты"] = []
            elif output_kind == "timing_verdict":
                base.update({"вердикт": "РАНО", "отыграно_pct": None,
                             "лаг_переноса": None, "триггер_ожидания": "войти при подтверждении срезом"})
            elif output_kind == "manipulation_score":
                base.update({"балл": 0, "сработавшие_детекторы": [], "переворот": False})
            elif output_kind == "control":
                base.update({"вердикт": "ВОЗВРАТ", "находки": []})
                base["вероятность"] = None
            return base

        prob = round(0.45 + (h % 41) / 100.0, 2)         # 0.45..0.85
        direction = "лонг" if (h >> 3) % 2 == 0 else "шорт"
        grounds = [
            {"факт": f"последняя котировка {sym} в срезе", "источник": ground_src,
             "значение": "см. market_slice.quotes"},
            {"факт": f"свежие новостные заголовки по теме {sym}", "источник": "context:news",
             "значение": "см. market_slice.news"},
        ]
        base = {
            "вывод": f"{agent_id}: сигнал по {sym} ({direction}) [mock]",
            "вероятность": prob,
            "base_rate": 0.5,
            "уверенность": "средняя" if prob > 0.6 else "низкая",
            "данные_основания": grounds,
            "что_неизвестно": ["mock: реальное рассуждение не выполнялось (дымовой тест формата)"],
        }
        if output_kind == "school_judgment":
            base["кандидаты"] = [{
                "актив": sym, "направление": direction,
                "тезис": f"{agent_id} mock-тезис по {sym}",
                "горизонт": "недели",
                "разрешимость": f"{sym} закроется {'выше' if direction=='лонг' else 'ниже'} "
                                f"текущей цены на горизонте 2 недель по данным EODHD",
            }]
        elif output_kind == "timing_verdict":
            verdict = ["РАНО", "ВОВРЕМЯ", "ПОЗДНО", "ЛОВУШКА"][h % 4]
            base.update({"вердикт": verdict, "отыграно_pct": h % 100,
                         "лаг_переноса": None if verdict != "РАНО" else "5 торговых дней (mock)",
                         "триггер_ожидания": None})
        elif output_kind == "manipulation_score":
            score = h % 11
            base.update({"балл": score,
                         "сработавшие_детекторы": ["источник_одиночка"] if score >= 7 else [],
                         "переворот": score >= 7})
        elif output_kind == "control":
            base.update({"вердикт": "OK", "находки": [
                {"объект": sym, "статус": "проверен", "обоснование": "mock: формат соблюдён"}]})
            base["вероятность"] = None
        return base

    def _synthesize_debate(self, agent_id, output_kind, sym, h, ground_src, forced_dir=None):
        """Валидные mock-суждения блоков E/F на конкретной идее (этапы 5–6 воронки)."""
        sym = sym or "BNO.US"  # дебаты всегда о конкретной идее; держим тикер из универсума
        direction = forced_dir or ("лонг" if (h >> 4) % 2 == 0 else "шорт")
        base_rate = 0.5
        prob = round(0.45 + (h % 36) / 100.0, 2)  # 0.45..0.80
        grounds = [
            {"факт": f"котировка и индикаторы {sym} в деле", "источник": ground_src,
             "значение": "см. market_slice"},
            {"факт": "издержки round_trip_bps по идее", "источник": "config/costs.yaml",
             "значение": "round_trip из costs"},
        ]
        unknown = ["mock: реальное рассуждение не выполнялось (дымовой тест формата/потока)"]

        if output_kind == "generator_hypothesis":
            return {
                "вывод": f"гипотеза по {sym} ({direction}) [mock]",
                "направление": direction,
                "вероятность": prob, "base_rate": base_rate,
                "уверенность": "средняя" if prob > 0.6 else "низкая",
                "каскадная_цепочка": [
                    "1-й порядок: прямое событие (отбрасываем как отыгранное)",
                    f"2-й порядок: перенос на {sym}",
                    f"торгуемое звено: {sym} {direction}"],
                "кто_продаёт_нам": "противоположная сторона — те, кто отыгрывает только 1-й порядок [mock]",
                "разрешимость": f"{sym} закроется {'выше' if direction=='лонг' else 'ниже'} цены входа "
                                f"на горизонте 2 недель по данным EODHD (§9)",
                "данные_основания": grounds, "что_неизвестно": unknown,
            }
        if output_kind == "critique":
            return {
                "вывод": f"red team по {sym}: тезис частично уязвим [mock]",
                "вероятность": round(max(0.2, prob - 0.15), 2),
                "уверенность": "средняя",
                "ошибки": ["mock: возможна подмена каскада 1-м порядком"],
                "скрытые_допущения": ["перенос дойдёт в заявленный лаг"],
                "альтернативные_объяснения": ["движение объясняется общим риск-офф"],
                "сильнейшее_возражение": "лаг переноса не подтверждён данными среза",
                "данные_основания": grounds, "что_неизвестно": unknown,
            }
        if output_kind == "advocacy":
            return {
                "вывод": f"защита тезиса по {sym}: ключевое возражение отбито частично [mock]",
                "вероятность": round(min(0.85, prob + 0.05), 2),
                "уверенность": "средняя",
                "ответы_на_критику": [
                    {"возражение": "лаг не подтверждён", "ответ": "лаг из knowledge/causal_links", "отбито": True},
                    {"возражение": "общий риск-офф", "ответ": "эффект сверх беты не показан", "отбито": False}],
                "данные_основания": grounds, "что_неизвестно": unknown,
            }
        if output_kind == "judge_verdict":
            # mock судья: баллы по 6 критериям рубрики ~3–4 → обычно УСТОЯЛА; зависит от h
            crit_ids = ["causal_chain_strength", "source_quality_independence", "timing_accounted",
                        "anti_manipulation_passed", "resolvability_p9", "net_asymmetry"]
            scores = [3 + ((h >> (i + 1)) % 2) for i in range(len(crit_ids))]  # 3 или 4
            mean = sum(scores) / len(scores)
            verdict = "УСТОЯЛА" if mean >= 3.0 else "РАЗБИТА"
            return {
                "вывод": f"вердикт по {sym}: идея {'устояла' if verdict=='УСТОЯЛА' else 'разбита'} [mock]",
                "вероятность": prob, "base_rate": base_rate,
                "уверенность": "средняя", "вердикт": verdict,
                "рубрика": {"version": "mock", "оценки": [
                    {"критерий": c, "балл": s, "обоснование": "mock: по существу дела"}
                    for c, s in zip(crit_ids, scores)]},
                "кто_продаёт_нам_и_почему_неправ": "1-й-порядковые продавцы недооценивают каскад [mock]",
                "почему_возможность_ещё_существует": "дальнее звено каскада ещё не связано рынком [mock]",
                "решающие_аргументы_за": ["A", "C"], "решающие_аргументы_против": ["B"],
                "неразрешённые_противоречия": [],
                "данные_основания": grounds, "что_неизвестно": unknown,
            }
        if output_kind == "risk_assessment":
            return {
                "вывод": f"риск по {sym} ({direction}): управляем при микроразмере [mock]",
                "вероятность": None, "уверенность": "средняя",
                "net_матожидание": "положительно после round_trip_bps при удержании 2 нед. [mock-оценка]",
                "сценарии": [
                    {"исход": "заработать", "вероятность": prob, "величина": "+ATR×2", "условие": "перенос дойдёт"},
                    {"исход": "потерять", "вероятность": round(1 - prob, 2), "величина": "-ATR×1", "условие": "лаг не сработал"}],
                "асимметрия": "апсайд≈2×даунсайд (mock)",
                "шорт_режим": ("неогр. убыток / маржин-колл / риск сквиза; borrow=нет данных → издержки занижены"
                              if direction == "шорт" else "не шорт"),
                "варианты_балансировки": ["меньший размер", "частичный вход", "хедж через смежный актив"],
                "рекомендация_снизить_риск": direction == "шорт",
                "сценарии_инвалидации": [f"идея неверна, если {sym} закроет окно входа без переноса"],
                "данные_основания": grounds, "что_неизвестно": unknown + (
                    ["short_borrow_fee_bps=null (П8): для шорта истинные издержки занижены"]
                    if direction == "шорт" else []),
            }
        if output_kind == "report":
            return {
                "вывод": f"итоговый отчёт по {sym} ({direction}) [mock]",
                "вероятность": prob, "уверенность": "средняя",
                "поля": {
                    "1_актив_направление_инструмент": f"{sym} {direction} (спот-прокси ETF)",
                    "2_каскадная_цепочка": ["1-й порядок отыгран", f"перенос на {sym}"],
                    "3_вероятность_и_калибровка": f"P судьи={prob} от base_rate=0.5; калибровка системы не доказана",
                    "4_сценарии_и_асимметрия_net": "апсайд≈2×даунсайд после издержек [mock]",
                    "5_отыгранность_стадия_входа": "часть хода отыграна; стадия — спец-источник [mock]",
                    "6_кто_продаёт_нам_и_почему_неправ": "1-й-порядковые продавцы недооценивают каскад",
                    "7_манипуляционный_балл_и_диагноз": "балл ниже порога (см. поле суждений)",
                    "8_варианты_балансировки_риска": ["меньший размер", "частичный вход"],
                    "9_балл_скоринга_разбивка_и_позиции_критика_судьи": "см. scoring_breakdown в деле",
                    "10_источники_с_credibility": ["EODHD котировки", "GDELT/NewsAPI новости"],
                    "11_что_неизвестно": unknown,
                    "12_сценарии_инвалидации": [f"{sym}: окно входа закрылось без переноса"],
                    "13_рамка": "исследовательский инструмент, не инвестиционная рекомендация",
                },
                "данные_основания": grounds, "что_неизвестно": unknown,
            }
        raise ValueError(f"mock: неизвестный debate output_kind {output_kind}")


def make_client(mode="auto", models=None, run_id=None):
    """Фабрика клиента. mode: 'live' | 'mock' | 'auto' (live при наличии ключа, иначе mock)."""
    models = models or load_models()
    if mode == "mock":
        return MockClient(models, run_id)
    if mode == "live":
        return LiveClient(models, run_id, mode="live")
    # auto
    if os.environ.get("OPENROUTER_API_KEY"):
        return LiveClient(models, run_id, mode="live")
    return MockClient(models, run_id)
