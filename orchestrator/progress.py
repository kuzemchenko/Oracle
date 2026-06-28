# -*- coding: utf-8 -*-
"""orchestrator/progress.py — «пульс» долгого прогона воронки для наблюдаемости (§15).

Воронка раньше молчала от старта до конца (10–30 мин) — для пользователя это «чёрный ящик».
Этот модуль пишет лёгкий heartbeat в journal/_run_progress.json на каждом этапе: какой
прогон, какое событие из K, какой этап, сколько прошло и СКОЛЬКО ОСТАЛОСЬ (оценка по доле
выполненного). Бот (/progress) и терминал читают его и показывают строку прогресса.

ПРИНЦИПЫ:
  • stdlib-only, без зависимостей проекта (бот импортирует format_line без тяжёлого графа);
  • любой сбой записи/чтения ПОГЛОЩАЕТСЯ (best-effort) — прогресс НИКОГДА не роняет воронку;
  • запись атомарная (tmp + os.replace);
  • это НЕ запечатанный журнал (П16 не касается): чисто оперативный индикатор, перезаписывается.

Модель доли выполнения:
  глобальная_доля = (индекс_события + доля_внутри_воронки) / всего_событий
  доля_внутри_воронки складывается из весов этапов (поле суждений — самый долгий шаг,
  внутри него интерполируем по числу опрошенных агентов). ETA = прошло·(1-доля)/доля.
"""
import os
import json
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / "journal" / "_run_progress.json"

# Этапы воронки run_funnel В ПОРЯДКЕ ИСПОЛНЕНИЯ + веса (~доли времени прогона).
# Сумма = 1.0. Поле суждений (21 LLM-агент последовательно) — самый тяжёлый шаг.
_FUNNEL_PHASES = [
    ("field",      "поле суждений (21 школа)", 0.55),
    ("candidates", "сбор кандидатов",          0.01),
    ("scan",       "скан + FDR",               0.02),
    ("coarse",     "грубый фильтр",            0.12),
    ("scoring",    "скоринг §7",               0.02),
    ("debates",    "дебаты (слепой суд)",      0.23),
    ("synthesis",  "синтез отчёта §8",         0.05),
]
_PHASE_LABEL = {k: lab for k, lab, _ in _FUNNEL_PHASES}
_PHASE_W = {k: w for k, _, w in _FUNNEL_PHASES}
_PHASE_ORDER = [k for k, _, _ in _FUNNEL_PHASES]


def _phase_offset(key):
    """Сумма весов этапов ДО `key` (доля воронки, пройденная к началу этапа)."""
    off = 0.0
    for k in _PHASE_ORDER:
        if k == key:
            return off
        off += _PHASE_W.get(k, 0.0)
    return off


# Состояние процесса-прогона держим в памяти модуля (один прогон на процесс).
_run = None


def _write(d):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE)
    except Exception:
        pass  # best-effort: индикатор не имеет права ронять воронку


def _flush(note=None, finished=False, summary=None):
    if _run is None:
        return
    try:
        now = time.time()
        outer_total = max(int(_run.get("outer_total") or 1), 1)
        outer_i = int(_run.get("outer_i") or 0)
        within = float(_run.get("within") or 0.0)
        if finished:
            frac = 1.0
        else:
            frac = (outer_i + within) / outer_total
            frac = max(0.0, min(frac, 0.999))
        elapsed = max(now - _run["t0"], 0.0)
        eta = (elapsed * (1.0 - frac) / frac) if frac > 0.02 else None
        _write({
            "run_id": _run["run_id"],
            "mode": _run["mode"],
            "title": _run["title"],
            "outer_total": outer_total,
            "outer_i": outer_i,
            "outer_label": _run.get("outer_label"),
            "phase": _run.get("phase"),
            "phase_label": _run.get("phase_label"),
            "agents": _run.get("agents"),          # [done, total] на этапе field
            "note": note if note is not None else _run.get("note"),
            "frac": round(frac, 4),
            "pct": int(round(frac * 100)),
            "elapsed_sec": round(elapsed, 1),
            "eta_sec": (round(eta, 1) if eta is not None else None),
            "started_at": _run["started_iso"],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "finished": bool(finished),
            "summary": summary,
        })
    except Exception:
        pass


# ── Публичный API (всё best-effort, исключения поглощаются) ──────────────────────

def active():
    """Идёт ли прогон В ЭТОМ процессе (begin был, finish ещё нет)."""
    return _run is not None and not _run.get("_finished")


def begin(run_id, mode, title, outer_total=1, note=None):
    """Старт прогона. outer_total — число событий (для event-first = K), иначе 1."""
    global _run
    try:
        t = time.time()
        _run = {
            "run_id": run_id, "mode": mode, "title": title,
            "outer_total": max(int(outer_total or 1), 1), "outer_i": 0,
            "outer_label": None, "phase": None, "phase_label": None,
            "agents": None, "within": 0.0, "note": note,
            "t0": t, "started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
            "_finished": False,
        }
        _flush(note=note)
    except Exception:
        pass


def set_outer_total(n):
    """Уточнить число событий после скана (источников может быть < K)."""
    try:
        if _run is not None:
            _run["outer_total"] = max(int(n or 1), 1)
            _flush()
    except Exception:
        pass


def note(msg):
    """Свободная заметка (напр. «скан событий…» до входа в первую воронку)."""
    try:
        if _run is not None:
            _run["note"] = msg
            _flush(note=msg)
    except Exception:
        pass


def outer(i, label):
    """Вход в обработку события i (0-based) из outer_total."""
    try:
        if _run is not None:
            _run["outer_i"] = int(i)
            _run["outer_label"] = label
            _run["within"] = 0.0
            _run["phase"] = None
            _run["phase_label"] = None
            _run["agents"] = None
            _flush()
    except Exception:
        pass


def phase(key, agents=None, detail=None):
    """Этап воронки внутри текущего события. Для 'field' agents=(done,total) — интерполяция."""
    try:
        if _run is None:
            return
        off = _phase_offset(key)
        if key == "field" and agents:
            done, total = int(agents[0]), max(int(agents[1]), 1)
            within = off + (done / total) * _PHASE_W.get("field", 0.0)
            _run["agents"] = [done, total]
        else:
            within = off  # этап только начался
            _run["agents"] = None
        # монотонность: доля не падает назад
        _run["within"] = max(float(_run.get("within") or 0.0), within)
        _run["phase"] = key
        _run["phase_label"] = _PHASE_LABEL.get(key, key)
        _flush(note=detail)
    except Exception:
        pass


def finish(summary=None):
    """Завершение прогона. summary — короткий человекочитаемый итог."""
    global _run
    try:
        if _run is not None:
            _run["_finished"] = True
            _flush(finished=True, summary=summary)
    except Exception:
        pass


# ── Чтение/форматирование (для бота и терминала; тоже best-effort) ────────────────

def read_state():
    try:
        if STATE.exists():
            return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _human_dur(sec):
    try:
        sec = int(round(sec))
        if sec < 60:
            return f"{sec}с"
        m, s = divmod(sec, 60)
        if m < 60:
            return f"{m}м" + (f" {s:02d}с" if s else "")
        h, m = divmod(m, 60)
        return f"{h}ч {m:02d}м"
    except Exception:
        return "—"


def _bar(frac, width=12):
    try:
        n = int(round(max(0.0, min(frac, 1.0)) * width))
        return "█" * n + "░" * (width - n)
    except Exception:
        return ""


def format_line(stale_after=420):
    """Человекочитаемая строка прогресса для бота. stale_after — сек до пометки «оборвался»."""
    d = read_state()
    if not d:
        return "Сейчас активного прогона нет. Запущу — покажу прогресс здесь."
    title = d.get("title") or "прогон"
    elapsed = _human_dur(d.get("elapsed_sec") or 0)

    if d.get("finished"):
        summ = d.get("summary") or "готово"
        return f"✅ Прогон завершён за {elapsed}.\n{summ}"

    # признак обрыва: давно не обновлялся
    try:
        upd = time.mktime(time.strptime(d.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
        stale = (time.time() - upd) > stale_after
    except Exception:
        stale = False

    ot, oi = d.get("outer_total") or 1, (d.get("outer_i") or 0) + 1
    where = f" · событие {min(oi, ot)}/{ot}" if ot > 1 else ""
    if d.get("outer_label"):
        where += f" ({d['outer_label']})"
    phase = d.get("phase_label")
    ag = d.get("agents")
    if phase == _PHASE_LABEL.get("field") and ag:
        phase = f"{phase} — {ag[0]}/{ag[1]}"
    step = f"\n   этап: {phase}" if phase else (f"\n   {d['note']}" if d.get("note") else "")
    eta = d.get("eta_sec")
    eta_s = f" · осталось ~{_human_dur(eta)}" if eta is not None else " · осталось: оцениваю…"
    head = "⏳ Ищу идеи" + where
    body = f"{step}\n   прошло {elapsed}{eta_s} · {d.get('pct', 0)}%\n   [{_bar(d.get('frac', 0))}]"
    if stale:
        body += "\n   ⚠ давно нет обновлений — возможно, прогон оборвался."
    return head + body


if __name__ == "__main__":
    print(format_line())
