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


# ── Логирование ─────────────────────────────────────────────────────────────────
def log_cost(mode, agent_id, model, usage, cost, ok=True, run_id=None):
    """Строка в journal/costs.jsonl (limits.yaml: costs_log). Append-only."""
    COSTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _now(), "mode": mode, "agent": agent_id, "model": model,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "cost_usd": cost, "ok": ok, "run_id": run_id,
    }
    with open(COSTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def log_model_swap(run_id, agent_id, role, attempted, used, reason):
    """Смена модели (фолбек) → протокол прогона (§26: важно, кто судил)."""
    FUNNEL_LOGS.mkdir(parents=True, exist_ok=True)
    path = FUNNEL_LOGS / f"{run_id}_model_swaps.jsonl"
    rec = {"ts": _now(), "agent": agent_id, "role": role,
           "attempted": attempted, "used": used, "reason": reason}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Базовый клиент ──────────────────────────────────────────────────────────────
class BaseClient:
    mode = "base"

    def __init__(self, models=None, run_id=None):
        self.models = models or load_models()
        self.run_id = run_id or "run"

    def complete(self, role, system, user, *, agent_id, output_kind):
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
        text = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage") or {}
        cost = usage.get("cost")  # OpenRouter возвращает реальную стоимость при usage.include
        return text, usage, cost

    def complete(self, role, system, user, *, agent_id, output_kind):
        role_cfg = resolve_role(role, self.models)
        chain = _model_chain(role_cfg)
        last_err = None
        for i, model in enumerate(chain):
            try:
                text, usage, cost = self._one_call(model, role_cfg, system, user)
                if i > 0:
                    log_model_swap(self.run_id, agent_id, role, chain[0], model,
                                   reason=str(last_err))
                log_cost(self.mode, agent_id, model, usage, cost, ok=True, run_id=self.run_id)
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

    def complete(self, role, system, user, *, agent_id, output_kind):
        role_cfg = resolve_role(role, self.models)
        model = role_cfg["primary"]
        payload = self._synthesize(agent_id, output_kind, user)
        text = json.dumps(payload, ensure_ascii=False)
        # mock НЕ пишет в journal/costs.jsonl: это не трата, бюджет (§30) считает только live.
        return {"text": text, "model": model, "usage": {}, "cost": 0.0, "fallback_index": 0}

    def _synthesize(self, agent_id, output_kind, user):
        h = self._h(agent_id, output_kind, len(user))
        abstain = (h % self.abstain_every == 0)

        # источники берём из реального среза (user-промпт), чтобы П8 проходил честно:
        # ищем тикеры универсума, упомянутые в срезе.
        ground_src = "context:market_slice"
        sym = None
        for s in ("BNO.US", "USO.US", "SPY.US", "DBC.US", "CPER.US", "COPX.US"):
            if s in user:
                sym = s
                break

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
