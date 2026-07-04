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
    """Свежайший СВОДНЫЙ event-first протокол (ef_<ts>.json, без '__' — как в боте).
    None — протоколов нет. Битый ПОСЛЕДНИЙ протокол НЕ подменяется предыдущим молча
    (кросс-ревью П3 №1, HIGH) — возвращается {"_битый_протокол": имя} для честного отказа."""
    d = pathlib.Path(logs_dir) if logs_dir else LOGS
    if not d.exists():
        return None
    cands = sorted(p for p in d.glob("ef_*.json") if "__" not in p.name)
    if not cands:
        return None
    latest = cands[-1]
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"_битый_протокол": latest.name}


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


def _dict(x):
    """Кривой (не-dict) фрагмент протокола → {} (кросс-ревью П3 №1: сессия не падает на мусоре)."""
    return x if isinstance(x, dict) else {}


def _list(x):
    """Кривое (не-list) списковое поле → [] (кросс-ревью П3 №2: money_трек=1 не роняет сессию)."""
    return x if isinstance(x, list) else []


def _idea_from_brief(brief, суд):
    """Идея сессии из брифа узла графа (money/провизорный/топ-K)."""
    brief = _dict(brief)
    asset = brief.get("актив")
    v = _dict(_dict(суд).get(asset))
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
    ci = _dict(ci)
    return {
        "актив": ci.get("актив"),
        "направление": None,
        "источник": "LLM-картограф (событие вне реестра тем)",
        "аргумент": {
            "событие": ci.get("событие"),
            "узлы_каскада": [
                {"порядок": n.get("порядок"), "узел": n.get("узел"), "чокпоинт": n.get("чокпоинт")}
                for n in _list(ci.get("узлы_каскада")) if isinstance(n, dict)][:4],
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
    if protocol.get("_битый_протокол"):
        return {"ОТКАЗ": f"последний протокол нечитаем ({protocol['_битый_протокол']}) — "
                         f"не подменяю его старым; запусти /run-funnel"}
    age = _protocol_age_days(protocol, asof)
    # Кросс-ревью П3 №2 (BLOCKER): свежесть должна быть ДОКАЗАНА. Нечитаемый ts (age=None) и
    # «будущий» ts (age<0 — сломанные часы/подделка) — отказ, а не выдача идей без гейта.
    if age is None:
        return {"ОТКАЗ": "у протокола нечитаемое время (ts) — свежесть не доказать; "
                         "запусти /run-funnel", "run_id_источника": protocol.get("run_id")}
    if age < 0:
        return {"ОТКАЗ": f"время протокола в будущем (возраст {round(age, 2)} дн) — часы/данные "
                         f"не в порядке; запусти /run-funnel", "run_id_источника": protocol.get("run_id")}
    if age > PROTOCOL_MAX_AGE_DAYS:
        # Кросс-ревью П3 №1 (BLOCKER): устаревший прогон НЕ упаковывается в сессию —
        # «греть вчерашнее» нечестно; честный отказ + приглашение к свежему прогону.
        return {"ОТКАЗ": f"последний прогон был {round(age, 1)} дн назад (порог "
                         f"{PROTOCOL_MAX_AGE_DAYS} дн) — идеи устарели; запусти /run-funnel",
                "run_id_источника": protocol.get("run_id"), "возраст_прогона_дней": round(age, 2)}
    g = _dict(protocol.get("граф_отбор"))
    суд = _dict(g.get("суд_money"))
    # ПРАВИЛО СЕССИИ (явное, не скрытая фильтрация — кросс-ревью П3 №1): один актив — одна
    # карточка; берётся вхождение из СТАРШЕГО трека (money > провизорный > картограф), потому что
    # оно несёт больше проверенных данных (суд/edge). Схлопнутые дубли честно считаются и помечаются.
    ideas, seen, dups = [], {}, 0
    def _add(idea):
        nonlocal dups
        a = idea.get("актив")
        if not a:
            return
        if a in seen:
            dups += 1
            seen[a].setdefault("также_в", []).append(idea.get("источник"))
            return
        seen[a] = idea
        ideas.append(idea)
    for brief in _list(g.get("money_трек")):
        _add(_idea_from_brief(brief, суд))
    for brief in _list(g.get("провизорный_трек")):
        _add(_idea_from_brief(brief, суд))
    for ci in _list(protocol.get("картограф_идеи")):
        _add(_idea_from_carto(ci))
    ideas = ideas[:n_max]
    return {
        "ts": asof,
        "run_id_источника": protocol.get("run_id"),
        "возраст_прогона_дней": (None if age is None else round(age, 2)),
        "прогон_устарел": False,                    # устаревший уже отсеян отказом выше
        "идей": len(ideas),
        "схлопнуто_дублей_актива": dups,            # правило «один актив — одна карточка»
        "мало_идей": len(ideas) < SESSION_MIN,      # честно, не добираем выдумками (П8)
        "идеи": ideas,
        "spec_ref": "§17.6 сессия партнёра (REVISION_2026-07 §R3); порядок = выдача прогона (не П2б)",
    }


def record_session(session, path=None):
    """Журнал сессий (append-only): факт показа — кормит retention/§R5. Возвращает запись.
    Отказ сессией НЕ является и НЕ журналируется (кросс-ревью П3 №1, LOW)."""
    if not session or session.get("ОТКАЗ"):
        return None
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
    n_sessions_7d = 0
    try:
        lo7 = _p(asof) - dt.timedelta(days=7)
        for rec in _iter_jsonl(sessions_path or SESSIONS_PATH):
            try:
                if lo7 <= _p(rec.get("ts")) <= hi:
                    n_sessions_7d += 1
            except (ValueError, TypeError):
                continue
    except (ValueError, TypeError):
        pass
    n_challenges = survived = judged = no_critic = 0
    for f in (sorted(d.glob("challenge_*.json")) if d.exists() else []):
        try:
            proto = json.loads(f.read_text(encoding="utf-8"))
            if not (lo <= _p(proto.get("ts")) <= hi):
                continue
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            continue
        n_challenges += 1
        исход = ((proto.get("дебаты") or {}).get("вердикт") or {}).get("исход")
        # УСТОЯЛА_БЕЗ_КРИТИКА — суд БЕЗ ред-тима (Инв#2 не выполнен): не в числителе И не в
        # знаменателе выживаемости (кросс-ревью П3 №1, HIGH) — отдельный честный счётчик.
        if исход == "УСТОЯЛА_БЕЗ_КРИТИКА":
            no_critic += 1
        elif исход in ("УСТОЯЛА", "РАЗБИТА"):
            judged += 1
            if исход == "УСТОЯЛА":
                survived += 1
    return {"окно_дней": days,
            "сессий": n_sessions,
            "сессий_за_7дн": n_sessions_7d,
            "retention_ok": n_sessions_7d >= 1,          # §R5: ≥1 сессия/НЕДЕЛЮ независимо от окна
            "докапываний": n_challenges,
            "выживаемость": (round(survived / judged, 3) if judged else None),
            "вердиктов": judged,
            "без_критика": no_critic,
            "spec_ref": "§R5 метрики П3"}
