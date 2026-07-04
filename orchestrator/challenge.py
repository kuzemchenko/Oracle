# -*- coding: utf-8 -*-
"""orchestrator/challenge.py — точечный состязательный разбор ОДНОЙ идеи по возражению владельца.

Это НЕ воронка §6. Берём конкретную идею (из последнего протокола journal/funnel_logs/ или
заданную вручную), вшиваем текстовое возражение/сомнение пользователя в дело и прогоняем
состязательный контур orchestrator.debate.run_debate:
    Генератор/Адвокат ЗАЩИЩАЮТ тезис → Критик/Red Team АТАКУЕТ (усиленный возражением) →
    Reviewer данных проверяет факты → СЛЕПОЙ Судья выносит вердикт по рубрике.

Возражение владельца идёт ДВАЖДЫ (решение пользователя §30): как обязательная линия атаки
критика И как отдельный вопрос судье. Слепота судьи и развязка семейств (П10) сохраняются —
возражение это данные дела, а не идентичность автора.

Инварианты: П8 (агенты не выдумывают; «нет данных» легитимно), П10 (судья ≠ семья генератора),
рубрика версионируема, вердикт пересчитывается кодом (всё внутри run_debate). Каждый разбор
журналируется в journal/challenges/{run_id}.json — ничего не удаляется (CLAUDE.md «Стиль»).
"""
import re
import json
import pathlib
import sqlite3
import datetime
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"
CHALLENGE_LOGS = ROOT / "journal" / "challenges"

import sys
sys.path.insert(0, str(ROOT))
from orchestrator import context as C          # noqa: E402
from orchestrator import openrouter as OR      # noqa: E402
from orchestrator import debate as DBT         # noqa: E402
from orchestrator import synthesis as SY       # noqa: E402


def _now_compact():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── чтение выданных идей из протоколов воронки ────────────────────────────────────
_TS_RE = re.compile(r"\d{8}T\d{6}Z")


def _ts_key(protocol):
    """Хронологический ключ: timestamp из ts/run_id боевого прогона. Статичные тест-фикстуры
    (week7_testday и т.п.) без timestamp идут НИЖЕ реальных прогонов — чтобы «последняя идея»
    бралась из настоящего прогона, а не из артефакта тестов."""
    ts = (protocol or {}).get("ts") or ""
    if ts:
        return ts
    m = _TS_RE.search(str((protocol or {}).get("run_id") or ""))
    return m.group(0) if m else ""


def _scan_protocols(logs_dir=None):
    """Все протоколы в журнале, по возрастанию хронологии (timestamp из ts/run_id)."""
    d = pathlib.Path(logs_dir or FUNNEL_LOGS)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda pr: (_ts_key(pr), str(pr.get("run_id") or "")))
    return out


def _reports_of(protocol):
    return ((protocol or {}).get("этап6_синтез") or {}).get("отчёты") or []


def _fields_of(report_card):
    """13 полей §8 из карточки идеи (терпимо к уровню вложенности)."""
    rep = report_card.get("отчёт") or {}
    if not isinstance(rep, dict):
        return {}
    j = rep.get("judgment") if isinstance(rep.get("judgment"), dict) else {}
    f = j.get("поля") if isinstance(j.get("поля"), dict) else None
    return f or (rep.get("поля") if isinstance(rep.get("поля"), dict) else {})


def _field(fields, prefix):
    """Значение поля §8 по числовому префиксу ключа ('2_каскадная…' → prefix='2')."""
    for k, v in (fields or {}).items():
        if str(k).strip().startswith(str(prefix) + "_") or str(k).strip().startswith(str(prefix) + "."):
            return v
    return None


def _humanize(v, n=400):
    if v is None:
        return ""
    if isinstance(v, list):
        v = " → ".join(str(x) for x in v if x not in (None, ""))
    elif isinstance(v, dict):
        v = "; ".join(f"{k}: {val}" for k, val in v.items())
    s = " ".join(str(v).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def list_ideas(protocol=None, logs_dir=None):
    """Список выданных идей (актив/направление/тезис) из протокола (по умолчанию — последнего
    с непустой выдачей). Для подсказки пользователю «по какой идее спорим»."""
    if protocol is None:
        for p in reversed(_scan_protocols(logs_dir)):
            if _reports_of(p):
                protocol = p
                break
    ideas = []
    for r in _reports_of(protocol):
        f = _fields_of(r)
        ideas.append({
            "run_id": (protocol or {}).get("run_id"),
            "актив": r.get("актив"),
            "направление": r.get("направление"),
            "балл": r.get("балл"),
            "тезис": _humanize(_field(f, "1")) or r.get("актив"),
            "каскад": _humanize(_field(f, "2")),
        })
    return ideas


def find_idea(asset=None, run_id=None, logs_dir=None):
    """Найти карточку идеи: по run_id и/или активу. Без указания — первая идея последнего
    протокола с выдачей. Возвращает (report_card, protocol) или (None, None)."""
    protos = _scan_protocols(logs_dir)
    if run_id:
        protos = [p for p in protos if p.get("run_id") == run_id]
    for p in reversed(protos):
        reps = _reports_of(p)
        if not reps:
            continue
        if asset:
            a = asset.strip().upper()
            for r in reps:
                if a in str(r.get("актив", "")).upper():
                    return r, p
        else:
            return reps[0], p
    return None, None


def candidate_from_card(report_card):
    """Собрать кандидат для run_debate из карточки идеи §8 (без выдумок: только то, что в карточке).

    Котировки/индикаторы НЕ берём из карточки — их пересоберёт свежий build_context. Тезис и
    §9-разрешимость восстанавливаем из полей отчёта, чтобы дело судьи было когерентно выданному.
    """
    f = _fields_of(report_card)
    seal = report_card.get("запечатанный_прогноз_§9") or {}
    thesis_parts = [_humanize(_field(f, "1"), 160), _humanize(_field(f, "2"), 240)]
    thesis = " — ".join(x for x in thesis_parts if x) or report_card.get("актив")
    resolvability = (seal.get("прогноз_§9_preview")
                     or _humanize(_field(f, "3"), 240) or None)
    return {
        "актив": report_card.get("актив"),
        "направление": report_card.get("направление"),
        "тезис": thesis,
        "разрешимость": resolvability,
        "школа": report_card.get("школа") or "выдано воронкой",
    }


# ── основной прогон ───────────────────────────────────────────────────────────────
def run_challenge(doubt, *, asset=None, src_run_id=None, candidate=None, mode="auto",
                  write=True, logs_dir=None, out_dir=None, client=None):
    """Точечный состязательный разбор идеи по возражению `doubt`.

    Идею задаём ЛИБО готовым candidate (актив/направление/тезис/разрешимость), ЛИБО ссылкой
    (asset/src_run_id) на выданную идею из journal/funnel_logs. Возвращает протокол разбора
    (с человеческим резюме под ключом 'резюме'); при write=True пишет journal/challenges/{id}.json.
    """
    src = None
    if candidate is None:
        card, src = find_idea(asset=asset, run_id=src_run_id, logs_dir=logs_dir)
        if card is None:
            return {"ОТКАЗ": "идея не найдена",
                    "подсказка": "укажи актив из выданных идей или сделай прогон воронки",
                    "доступные_идеи": list_ideas(logs_dir=logs_dir)}
        candidate = candidate_from_card(card)
    if not candidate.get("актив"):
        return {"ОТКАЗ": "у идеи нет актива — нечего разбирать"}
    if not (doubt or "").strip():
        return {"ОТКАЗ": "пустое возражение — нечего проверять (П8)"}

    run_id = f"challenge_{_now_compact()}"
    theme = candidate["актив"]
    ctx = C.build_context(theme=theme)
    # Ревью 2026-07-04 H7: build_context знает только CORE-инструменты — разбор идеи по компании
    # каскада шёл с котировка=None/индикаторы=None, и судья штрафовал «нет данных» на пустом месте,
    # хотя история в oracle.db есть. Инъектируем котировку/индикаторы актива явно (как _vet_money).
    sym = candidate["актив"]
    if sym and sym not in (ctx.get("quotes") or {}) and C.DB.exists():
        con = sqlite3.connect(str(C.DB))
        try:
            q = C._quotes(con, sym)
            if q:
                ctx.setdefault("quotes", {})[sym] = {"last": q[-1], "n_bars": len(q),
                                                     "first_date": q[0]["date"], "last_date": q[-1]["date"]}
                ctx.setdefault("indicators", {})[sym] = C._indicators(q)
        finally:
            con.close()
    costs = SY.load_costs()
    cli = client or OR.make_client(mode=mode, run_id=run_id)

    debate = DBT.run_debate(candidate, ctx, cli, run_id=run_id, costs=costs, user_doubt=doubt)

    protocol = {
        "run_id": run_id,
        "ts": _now_iso(),
        "mode": getattr(cli, "mode", mode),
        "spec_ref": "§4 блок E (ad hoc): состязательный разбор идеи по возражению владельца",
        "источник_идеи": {"src_run_id": (src or {}).get("run_id"), "актив": candidate["актив"]},
        "идея": candidate,
        "возражение_владельца": doubt,
        "дебаты": debate,
    }
    protocol["резюме"] = summarize(protocol)
    if write:
        d = pathlib.Path(out_dir or CHALLENGE_LOGS)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{run_id}.json").write_text(
            json.dumps(protocol, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return protocol


# ── человеческое резюме разбора ───────────────────────────────────────────────────
def _say(rec):
    """Краткий вывод реплики агента (или честная пометка, что валидного ответа нет)."""
    if not rec or not rec.get("ok"):
        return "нет валидного ответа"
    j = rec.get("judgment") or {}
    return _humanize(j.get("вывод") or j.get("вердикт") or j.get("резюме"), 320) or "—"


# ── дайджест разборов для еженедельного разбора §25 (мостик «вопросы → предложения») ──
# ДЕТЕРМИНИРОВАННАЯ агрегация (math — не LLM): считаем повторяемость слабых критериев рубрики и
# пробелов в данных по live-разборам. Превращение в ПОПРАВКИ — отдельный человеко-управляемый шаг
# /review-week (§25) с одобрением пользователя (§10). Единичный разбор сам по себе ничего не меняет.
_MISSING_MARKERS = ("отсут", "нет данных", "не подтвержд", "не найден", "недоступ", "null")


def _rubric_scores(debate):
    j = ((debate.get("реплики") or {}).get("судья") or {}).get("judgment") or {}
    out = {}
    for o in ((j.get("рубрика") or {}).get("оценки") or []):
        b, c = o.get("балл"), o.get("критерий")
        if c is not None and isinstance(b, (int, float)) and not isinstance(b, bool):
            out[str(c)] = float(b)
    return out


def _missing_findings(debate):
    """Находки reviewer'а, помеченные как отсутствующие/неподтверждённые в данных — сигнал пробела."""
    rev = ((debate.get("реплики") or {}).get("reviewer_данных") or {}).get("judgment") or {}
    out = []
    for f in (rev.get("находки") or []):
        st = str(f.get("статус", "")).lower()
        if any(m in st for m in _MISSING_MARKERS):
            obj = f.get("объект") or f.get("обоснование")
            if obj:
                out.append(" ".join(str(obj).split())[:90])
    return out


def digest_challenges(since=None, logs_dir=None, break_threshold=3.0):
    """Агрегат live-разборов /debate для /review-week: счёт по вердиктам, повторяющиеся слабые
    критерии рубрики, пробелы в данных. mock-разборы ИСКЛЮЧЕНЫ (П16: не учимся на заглушках).

    since: ISO-строка — учитывать разборы с ts >= since (с прошлого разбора). Возвращает dict.
    """
    d = pathlib.Path(logs_dir or CHALLENGE_LOGS)
    files = sorted(d.glob("challenge_*.json")) if d.exists() else []
    разборы, n_mock = [], 0
    verdicts = defaultdict(int)
    crit_low, crit_sum, crit_n = defaultdict(int), defaultdict(float), defaultdict(int)
    gaps = defaultdict(lambda: {"частота": 0, "активы": set()})
    for f in files:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if since and str(p.get("ts", "")) < str(since):
            continue
        if p.get("mode") != "live":          # П16: mock-разборы не питают обучение
            n_mock += 1
            continue
        deb = p.get("дебаты") or {}
        asset = (p.get("идея") or {}).get("актив")
        исход = (deb.get("вердикт") or {}).get("исход")
        verdicts[исход] += 1
        low = []
        for c, b in _rubric_scores(deb).items():
            crit_sum[c] += b
            crit_n[c] += 1
            if b < break_threshold:
                crit_low[c] += 1
                low.append(c)
        for m in _missing_findings(deb):
            gaps[m]["частота"] += 1
            gaps[m]["активы"].add(asset)
        разборы.append({
            "run_id": p.get("run_id"), "ts": p.get("ts"), "актив": asset,
            "направление": (p.get("идея") or {}).get("направление"),
            "вердикт": исход, "средний_балл": (deb.get("вердикт") or {}).get("средний_балл_рубрики"),
            "возражение": p.get("возражение_владельца"), "слабые_критерии": low,
        })
    weak = [{"критерий": c, "n_низких": crit_low[c], "n_оценок": crit_n[c],
             "средний_балл": round(crit_sum[c] / crit_n[c], 2) if crit_n[c] else None}
            for c in crit_n]
    weak.sort(key=lambda x: (-x["n_низких"], x["средний_балл"] if x["средний_балл"] is not None else 99))
    gap_list = [{"сигнал": k, "частота": v["частота"], "активы": sorted(a for a in v["активы"] if a)}
                for k, v in gaps.items()]
    gap_list.sort(key=lambda x: -x["частота"])
    return {"n_разборов": len(разборы), "n_mock_пропущено": n_mock,
            "по_вердикту": dict(verdicts), "слабые_критерии_рубрики": weak,
            "пробелы_в_данных": gap_list, "разборы": разборы}


def format_digest(dg):
    """Человеческий рендер дайджеста для /review-week (вход для формулирования ПРЕДЛОЖЕНИЙ §10)."""
    if not dg.get("n_разборов"):
        return (f"Live-разборов /debate нет (mock пропущено: {dg.get('n_mock_пропущено', 0)}). "
                "Предлагать нечего.")
    lines = [f"Дайджест разборов /debate (live: {dg['n_разборов']}, "
             f"mock пропущено: {dg['n_mock_пропущено']}):",
             f"  по вердикту: {dg['по_вердикту']}",
             "  повторяющиеся слабые критерии рубрики (кандидаты на усиление промптов/рубрики):"]
    for c in dg["слабые_критерии_рубрики"]:
        if c["n_низких"]:
            lines.append(f"    • {c['критерий']}: низких {c['n_низких']}/{c['n_оценок']}, "
                         f"средний {c['средний_балл']}")
    lines.append("  повторяющиеся пробелы в данных (кандидаты на новые источники/коннекторы):")
    for g in dg["пробелы_в_данных"][:10]:
        lines.append(f"    • ×{g['частота']} {g['сигнал']} ({', '.join(g['активы']) or '—'})")
    lines.append("ДИСЦИПЛИНА §10: единичный сигнал — наблюдение, не поправка; меняем только при "
                 "повторяемости и с одобрением пользователя. Применение — /apply-weights.")
    return "\n".join(lines)


def summarize(protocol):
    """Резюме разбора человеческим языком: тезис, возражение, кто как ответил, вердикт судьи."""
    deb = protocol.get("дебаты") or {}
    if "ОТКАЗ" in protocol:
        return protocol["ОТКАЗ"]
    reps = deb.get("реплики") or {}
    v = deb.get("вердикт") or {}
    cand = protocol.get("идея") or {}

    direction = cand.get("направление") or "—"
    head = f"Разбор идеи: {cand.get('актив')} ({direction})"
    doubt = protocol.get("возражение_владельца") or "—"

    outcome = v.get("исход", "—")
    mean = v.get("средний_балл_рубрики")
    thr = v.get("порог")
    prob = v.get("вероятность_судьи")
    verdict_line = f"Вердикт слепого судьи: {outcome}"
    if mean is not None and thr is not None:
        verdict_line += f" (средний балл рубрики {mean} против порога {thr})"
    if outcome == "УСТОЯЛА" and isinstance(prob, (int, float)):
        verdict_line += f"; вероятность по судье ~{round(prob * 100)}%"
    if v.get("примечание"):
        verdict_line += f". {v['примечание']}"

    survived = {"УСТОЯЛА": "идея ВЫДЕРЖАЛА твоё возражение",
                "РАЗБИТА": "идея НЕ выдержала — возражение/критика перевесили",
                "ВЕТО": "процедурное вето: контур не довёл разбор (см. причину)"}.get(outcome, outcome)

    lines = [
        head,
        f"Семейства моделей: генератор={deb.get('семейство_генератора')}, "
        f"судья={deb.get('семейство_судьи')} (П10 — судья другого семейства).",
        "",
        f"🟢 Защита (генератор): {_say(reps.get('генератор'))}",
        f"🔴 Атака (критик, учёл твоё возражение): {_say(reps.get('критик'))}",
        f"🟢 Ответ (адвокат): {_say(reps.get('адвокат'))}",
        f"🔍 Проверка фактов (reviewer): {_say(reps.get('reviewer_данных'))}",
        "",
        f"Твоё возражение: «{_humanize(doubt, 280)}»",
        f"⚖️ {verdict_line}",
        f"Итог: {survived}.",
    ]
    return "\n".join(lines)
