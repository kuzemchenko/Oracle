# -*- coding: utf-8 -*-
"""orchestrator/partner_session.py — режим §17.6 «Сессия партнёра» (П3, REVISION_2026-07 §R3/§R4.3).

ЧТО ЭТО. Владелец говорит «дай идеи» — система отдаёт 3–5 идей ИЗ УЖЕ СДЕЛАННОГО прогона
(research-поток: картограф + треки графа), по каждой — готовый аргумент («новость → цепочка →
компания»), «кто продаёт нам и почему он неправ» (из слепого суда), поле «внимание» (П2а) и
честные ограничения. Дальше владелец докапывается существующими инструментами («разбери ТИКЕР»,
«/debate <возражение>»), решения фиксируются §12-журналом. Это УПАКОВКА потока в диалог —
НЕ новый мотор и НЕ новое ранжирование.

ГРАНИЦА П2б (слой Б, отдельная подпись владельца): здесь НЕТ собственной оси ранжирования.
Сессия берёт идеи В СУЩЕСТВУЮЩЕМ порядке выдачи прогона: money-трек (ранг воронки графа) →
провизорный → идеи картографа (ранг по тектоническому потенциалу — существующая сортировка
_proposal_ideas). Поле «внимание» показывается, но порядок НЕ меняет.

ЖУРНАЛ СЕССИЙ. journal/partner_sessions.jsonl (append-only): факт сессии + показанные активы —
кормит метрики §R5 (retention ≥1 сессии/нед; докапывания и выживаемость — по journal/challenges/).
Детерминизм (Инв#6): модуль не зовёт LLM; вся аргументация — из посчитанного прогоном (П8).
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOGS = ROOT / "journal" / "funnel_logs"
SESSIONS_PATH = ROOT / "journal" / "partner_sessions.jsonl"
CHALLENGES_DIR = ROOT / "journal" / "challenges"

SESSION_MIN = 3
SESSION_MAX = 5
PROTOCOL_MAX_AGE_DAYS = 3      # старее — честно предлагаем свежий прогон, а не греем вчерашнее


def load_latest_protocol(logs_dir=None):
    """Свежайший СВОДНЫЙ event-first протокол (ef_<ts>.json, без '__' — как в боте). None — нет."""
    d = pathlib.Path(logs_dir) if logs_dir else LOGS
    if not d.exists():
        return None
    cands = sorted(p for p in d.glob("ef_*.json") if "__" not in p.name)
    for p in reversed(cands):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue                       # битый протокол — берём предыдущий целый
    return None


def _protocol_age_days(protocol, asof):
    """Возраст протокола в днях относительно asof (ISO UTC); None — не определить."""
    import datetime as dt
    try:
        def _p(x):
            d = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
            return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d
        return (_p(asof) - _p(protocol.get("ts"))).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def _idea_from_brief(brief, суд):
    """Идея сессии из брифа узла графа (money/провизорный/топ-K)."""
    asset = brief.get("актив")
    v = (суд or {}).get(asset) or {}
    return {
        "актив": asset,
        "направление": brief.get("направление"),
        "источник": "граф каскадов" + (" (провизорный)" if brief.get("провизорный") else " (money-трек)"),
        "аргумент": {
            "цепочка": brief.get("цепочка"), "порядок_узла": brief.get("порядок"),
            "чокпоинт": brief.get("чокпоинт"), "неотыгранный_edge": brief.get("edge"),
            "надёжность_r2": brief.get("надёжность_r2"), "лаг_дней": brief.get("лаг_дней"),
        },
        "суд": ({"исход": v.get("исход"), "кто_продаёт_нам": v.get("кто_против"),
                 "почему_возможность": v.get("почему_возможность"),
                 "балл": v.get("балл")} if v else {"исход": None,
                                                   "пометка": "слепой суд по этой идее не гонялся"}),
        "внимание": brief.get("внимание"),
        "вероятность": brief.get("вероятность"),
    }


def _idea_from_carto(ci):
    """Идея сессии из истории картографа (research: новость → цепочка → компания)."""
    return {
        "актив": ci.get("актив"),
        "направление": None,
        "источник": "LLM-картограф (событие вне реестра тем)",
        "аргумент": {
            "событие": ci.get("событие"),
            "узлы_каскада": [
                {"порядок": n.get("порядок"), "узел": n.get("узел"), "чокпоинт": n.get("чокпоинт")}
                for n in (ci.get("узлы_каскада") or [])[:4]],
            "тектонический_потенциал": ci.get("тектонический_потенциал"),
            "отыгранность_узла": ci.get("отыгранность_узла"),
        },
        "суд": {"исход": None, "пометка": "research-гипотеза — суд не гонялся, вероятность не мерена"},
        "внимание": ci.get("внимание"),
        "вероятность": None,
    }


def build_session(protocol, *, asof, n_max=SESSION_MAX):
    """Собрать сессию из протокола прогона. Возвращает dict сессии (П8: только посчитанное).

    Порядок идей = СУЩЕСТВУЮЩИЙ порядок выдачи (граница П2б — см. докстринг модуля):
    money-трек → провизорный → картограф; дедуп по активу (первое вхождение богаче)."""
    if not protocol:
        return {"ОТКАЗ": "нет ни одного протокола прогона — сначала /run-funnel"}
    age = _protocol_age_days(protocol, asof)
    g = protocol.get("граф_отбор") or {}
    суд = g.get("суд_money") or {}
    ideas, seen = [], set()
    for brief in (g.get("money_трек") or []):
        if brief.get("актив") and brief["актив"] not in seen:
            seen.add(brief["актив"])
            ideas.append(_idea_from_brief(brief, суд))
    for brief in (g.get("провизорный_трек") or []):
        if brief.get("актив") and brief["актив"] not in seen:
            seen.add(brief["актив"])
            ideas.append(_idea_from_brief(brief, суд))
    for ci in (protocol.get("картограф_идеи") or []):
        if ci.get("актив") and ci["актив"] not in seen:
            seen.add(ci["актив"])
            ideas.append(_idea_from_carto(ci))
    ideas = ideas[:n_max]
    return {
        "ts": asof,
        "run_id_источника": protocol.get("run_id"),
        "возраст_прогона_дней": (None if age is None else round(age, 2)),
        "прогон_устарел": (age is not None and age > PROTOCOL_MAX_AGE_DAYS),
        "идей": len(ideas),
        "мало_идей": len(ideas) < SESSION_MIN,      # честно, не добираем выдумками (П8)
        "идеи": ideas,
        "spec_ref": "§17.6 сессия партнёра (REVISION_2026-07 §R3); порядок = выдача прогона (не П2б)",
    }


def record_session(session, path=None):
    """Журнал сессий (append-only): факт показа — кормит retention/§R5. Возвращает запись."""
    p = pathlib.Path(path) if path else SESSIONS_PATH
    rec = {"ts": session.get("ts"), "run_id_источника": session.get("run_id_источника"),
           "идей": session.get("идей"), "активы": [i.get("актив") for i in session.get("идеи", [])]}
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _iter_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue                     # битая строка журнала не валит метрики


def session_metrics(*, asof, days=7, sessions_path=None, challenges_dir=None):
    """Метрики §R5 за окно days до asof: retention (сессий), докапываний (challenge-разборов),
    выживаемость идей под докапыванием (доля УСТОЯЛА среди вынесенных вердиктов). Детерминированно."""
    import datetime as dt

    def _p(x):
        d = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d

    try:
        lo = _p(asof) - dt.timedelta(days=days)
        hi = _p(asof)
    except (ValueError, TypeError):
        return {"ошибка": f"нечитаемый asof: {asof!r}"}
    n_sessions = 0
    for rec in _iter_jsonl(sessions_path or SESSIONS_PATH):
        try:
            if lo <= _p(rec.get("ts")) <= hi:
                n_sessions += 1
        except (ValueError, TypeError):
            continue
    d = pathlib.Path(challenges_dir) if challenges_dir else CHALLENGES_DIR
    n_challenges = survived = judged = 0
    for f in (sorted(d.glob("challenge_*.json")) if d.exists() else []):
        try:
            proto = json.loads(f.read_text(encoding="utf-8"))
            if not (lo <= _p(proto.get("ts")) <= hi):
                continue
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            continue
        n_challenges += 1
        исход = ((proto.get("дебаты") or {}).get("вердикт") or {}).get("исход")
        if исход in ("УСТОЯЛА", "РАЗБИТА", "УСТОЯЛА_БЕЗ_КРИТИКА"):
            judged += 1
            if исход == "УСТОЯЛА":
                survived += 1
    return {"окно_дней": days,
            "сессий": n_sessions,
            "retention_ok": n_sessions >= 1,            # §R5: ≥1 сессия/нед — главный KPI
            "докапываний": n_challenges,
            "выживаемость": (round(survived / judged, 3) if judged else None),
            "вердиктов": judged,
            "spec_ref": "§R5 метрики П3"}
