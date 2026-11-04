# -*- coding: utf-8 -*-
"""ops/auto_review.py — АВТОПЕТЛЯ §25 (решение владельца 09.07.2026): система сама запускает
разборы и инсайты; корректировки НЕ применяет сама, а сообщает об их доступности.

Два режима:
  • без флагов (еженедельно, пятница 18:00 cron) — полный детерминированный разбор:
    Brier по трекам (весь срок + последние 7 дней), калибровочные корзины, худшие промахи
    недели, статистика слепого суда, засуха денежного трека, статус форвард-промоушена,
    покрытие «внимания» §R5. Отчёт → reports/auto_review/review_<дата>.md,
    сводка → journal/notices.jsonl (бот пушит владельцу).
  • --watch (ежедневно, 21:15 cron, после сверки 21:00) — дешёвый дозор: инсайт-алерты
    ТОЛЬКО при срабатывании условия и один раз на состояние (дедуп через
    journal/auto_review_state.json), чтобы не спамить.

ГРАНИЦЫ (порядок владельца «корректировка если доступна» СОВМЕЩЁН с П16/§25):
  • только детерминированный код (инвариант #6), LLM здесь нет;
  • журналы прогнозов/исходов ТОЛЬКО читает;
  • веса/промоушены НЕ применяет: петля §25 = предложения; --apply промоушена — рукой
    владельца, пока открыт долг B4(а) псевдорепликации корма (авто-apply завысил бы N
    дублями и обошёл бы гейт §10 нечестно). Доступная корректировка → уведомление в бота
    с просьбой подписи.
"""
import argparse
import datetime
import glob
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import sealing as SEAL            # noqa: E402
from mathlib import brier as BR                # noqa: E402
from orchestrator import resolve as RES        # noqa: E402

NOTICES = ROOT / "journal" / "notices.jsonl"
STATE = ROOT / "journal" / "auto_review_state.json"
REPORTS_DIR = ROOT / "reports" / "auto_review"
FUNNEL_GLOB = str(ROOT / "journal" / "funnel_logs" / "ef_*.json")
PROMO_REPORT = ROOT / "ops" / "reports" / "promotions" / "report.json"
EDGE_CANDIDATES = ROOT / "knowledge" / "edge_candidates.jsonl"   # Э4(ж): реестр кандидат-рёбер

WEEK_DAYS = 7
DROUGHT_ALERT_DAYS = 7          # засуха money-печати: первый алерт на 7-й день, далее раз в 7
COURT_STREAK_MIN = 4            # 100% РАЗБИТА при ≥N судах за неделю — это уже закономерность
PUSH_AGE_ALERT_DAYS = 2         # v3/V1 (аудит 03.11: было 4 и по ЛЮБОМУ коммиту): последний СЕРВЕРНЫЙ
                                # чекпоинт старше N дней = журналы сервера не доезжают до GitHub
ROADMAP_STATE = ROOT / "ops" / "roadmap_state.yaml"   # v3/V1: машинное состояние карты


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _notice(text, notices_path=None, silent=False):
    """Заметка в journal/notices.jsonl — бот пушит новые по курсору (тот же канал доставки, что alerts).
    silent=True — СЛУЖЕБНАЯ: пишется в журнал (её читают машинист и пятничный разбор), но владельцу в
    Telegram не уходит. Владелец 04.11 про «суд разбил 5/5»: «что за мусор и зачем?» — ему идут только
    заметки, где нужно его слово или действие."""
    path = pathlib.Path(notices_path) if notices_path else NOTICES
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now().isoformat(timespec="seconds"), "text": text}
    if silent:
        rec.update({"silent": True, "source": "дозор"})
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _load(predictions_path=None, outcomes_path=None):
    preds = SEAL.read_predictions(predictions_path)
    outs = RES.read_outcomes(outcomes_path)
    return preds, outs


def _split_by_track(rows):
    by = {}
    for r in rows:
        by.setdefault(RES.track_for_kind(r.get("kind")), []).append(r)
    return by


def _brier(rows):
    probs = [r.get("probability") for r in rows]
    outs = [r.get("outcome") for r in rows]
    pairs = [(p, o) for p, o in zip(probs, outs) if p is not None and o is not None]
    if not pairs:
        return None
    return round(BR.brier_score([p for p, _ in pairs], [o for _, o in pairs]), 4)


def _recent_iso(rows, key, days, now=None):
    """Строки с ISO-датой в поле key не старше days. Сравнение лексикографическое по ISO."""
    cutoff = ((now or _now()) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    return [r for r in rows
            if str(r.get(key) or "")[:19] >= cutoff]


def _days_since_money_seal(preds, now=None):
    """Дней с последней ДЕНЕЖНОЙ печати (§11-трек). None — печатей не было вовсе."""
    dates = [str(p.get("sealed_at") or p.get("ts") or "")[:10]
             for p in preds if RES.track_for_kind(p.get("kind")) == "money"]
    dates = [d for d in dates if d]
    if not dates:
        return None
    last = datetime.date.fromisoformat(max(dates))
    return ((now or _now()).date() - last).days


def _court_week(funnel_glob=None, days=WEEK_DAYS, now=None):
    """Слепой суд за неделю по протоколам event_first: сколько судили, сколько РАЗБИТА, причины."""
    cutoff = ((now or _now()) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    судов, разбито, вердикты = 0, 0, []
    money_кандидатов, money_запечатано = 0, 0
    for f in sorted(glob.glob(funnel_glob or FUNNEL_GLOB)):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if str(d.get("ts") or "")[:10] < cutoff:
            continue
        g = d.get("граф_отбор") or {}
        z = g.get("запечатано") or {}
        money_запечатано += int(z.get("money") or 0)
        money_кандидатов += len(g.get("money_трек") or [])
        for sym, v in (g.get("суд_money") or {}).items():
            if not isinstance(v, dict):
                continue
            судов += 1
            исход = v.get("исход")
            if исход == "РАЗБИТА":
                разбито += 1
            вердикты.append({"актив": sym, "исход": исход, "балл": v.get("балл"),
                             "дата": str(d.get("ts") or "")[:10],
                             "почему": str(v.get("почему_возможность") or "")[:160]})
    return {"судов": судов, "разбито": разбито, "вердикты": вердикты,
            "money_кандидатов": money_кандидатов, "money_запечатано": money_запечатано}


def _attention_week(funnel_glob=None, days=WEEK_DAYS, now=None):
    cutoff = ((now or _now()) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    cov = []
    for f in sorted(glob.glob(funnel_glob or FUNNEL_GLOB)):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if str(d.get("ts") or "")[:10] < cutoff:
            continue
        c = (d.get("внимание_покрытие") or {}).get("покрытие")
        if c is not None:
            cov.append(float(c))
    return round(sum(cov) / len(cov), 3) if cov else None


def _promotions_status(promo_path=None):
    path = pathlib.Path(promo_path) if promo_path else PROMO_REPORT
    if not path.exists():
        return {"есть_отчёт": False, "n_promote": 0}
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return {"есть_отчёт": False, "n_promote": 0}
    return {"есть_отчёт": True, "n_promote": int(d.get("n_promote") or 0),
            "n_edges": int(d.get("n_edges") or 0), "stats": d.get("stats"),
            "mtime": path.stat().st_mtime,
            "рёбра": sorted((d.get("promotions") or {}).keys())}


def _edge_candidates_week(candidates_path=None, days=WEEK_DAYS, now=None):
    """Э4(ж), решение владельца 13.07 №7: добавленные за неделю кандидат-рёбра «перебора мира»
    из knowledge/edge_candidates.jsonl (append-only) — для недельного отчёта §25."""
    path = pathlib.Path(candidates_path) if candidates_path else EDGE_CANDIDATES
    cutoff = ((now or _now()) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    total, week = 0, []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if str(r.get("ts") or "")[:10] >= cutoff:
                week.append({"ребро": r.get("edge_key"), "событие": r.get("событие"),
                             "сегмент": r.get("сегмент"), "дата": str(r.get("ts") or "")[:10]})
    return {"всего_в_реестре": total, "за_7д": len(week), "рёбра_7д": week}


def compute_review(predictions_path=None, outcomes_path=None, funnel_glob=None,
                   promo_path=None, now=None, candidates_path=None):
    """Все числа еженедельного разбора §25 — детерминированно, только чтение журналов."""
    now = now or _now()
    preds, outs = _load(predictions_path, outcomes_path)
    outs_by = _split_by_track(outs)
    preds_by = _split_by_track(preds)
    resolved_h = {o.get("hash") for o in outs}

    треки = {}
    for track in ("money", "provisional", "edge_forward", "calibration"):
        t_out = outs_by.get(track, [])
        t_out_7d = _recent_iso(t_out, "resolved_at", WEEK_DAYS, now)
        треки[track] = {
            "запечатано": len(preds_by.get(track, [])),
            "зреет": sum(1 for p in preds_by.get(track, []) if p.get("hash") not in resolved_h),
            "исходов": len(t_out),
            "сбылось": sum(1 for o in t_out if o.get("outcome") == 1),
            "brier": _brier(t_out),
            "исходов_7д": len(t_out_7d),
            "brier_7д": _brier(t_out_7d),
        }

    # худшие промахи недели (по всем трекам, кроме calibration-монетки): (p-исход)² максимален
    week_out = [o for t, rows in outs_by.items() if t != "calibration"
                for o in _recent_iso(rows, "resolved_at", WEEK_DAYS, now)]
    misses = [o for o in week_out
              if o.get("probability") is not None and o.get("outcome") is not None]
    misses.sort(key=lambda o: (float(o["probability"]) - float(o["outcome"])) ** 2, reverse=True)
    промахи = [{"актив": o.get("asset"), "трек": RES.track_for_kind(o.get("kind")),
                "P": o.get("probability"), "исход": o.get("outcome"),
                "направление": o.get("direction"), "срок": str(o.get("resolve_by") or "")[:10]}
               for o in misses[:5]]

    return {
        "ts": now.isoformat(timespec="seconds"),
        "треки": треки,
        "худшие_промахи_7д": промахи,
        "суд_7д": _court_week(funnel_glob, now=now),
        "money_засуха_дней": _days_since_money_seal(preds, now),
        "внимание_покрытие_7д": _attention_week(funnel_glob, now=now),
        "промоушен": _promotions_status(promo_path),
        "рёбра_кандидаты": _edge_candidates_week(candidates_path, now=now),
        "spec_ref": "§25 петля качества (автозапуск — решение владельца 09.07); §10 предложения",
    }


def _fmt_track(name, t):
    b = t.get("brier")
    b7 = t.get("brier_7д")
    return (f"  {name:12} печатей {t['запечатано']:3d} (зреет {t['зреет']}) · исходов {t['исходов']:3d}"
            f" · Brier {b if b is not None else '—'}"
            f" · за 7д: {t['исходов_7д']} исходов, Brier {b7 if b7 is not None else '—'}")


def render_md(r):
    суд = r["суд_7д"]
    lines = [
        f"# Авторазбор §25 — {r['ts'][:10]}",
        "",
        "Ориентир Brier: 0.25 = «монетка» (всегда 50/50); меньше — лучше.",
        "",
        "## Треки (весь срок / последние 7 дней)",
        _fmt_track("денежный", r["треки"]["money"]),
        _fmt_track("провизорный", r["треки"]["provisional"]),
        _fmt_track("B4-рёбра", r["треки"]["edge_forward"]),
        _fmt_track("калибровка", r["треки"]["calibration"]),
        "",
        "## Денежный трек",
        (f"Дней без денежной печати: {r['money_засуха_дней']}"
         if r["money_засуха_дней"] is not None else "Денежных печатей ещё не было."),
        (f"За 7 дней: кандидатов {суд['money_кандидатов']}, судов {суд['судов']}, "
         f"разбито {суд['разбито']}, запечатано {суд['money_запечатано']}."),
        "",
        "## Слепой суд — вердикты недели",
    ]
    for v in суд["вердикты"]:
        lines.append(f"- {v['дата']} {v['актив']}: {v['исход']} (балл {v['балл']}) — {v['почему']}")
    if not суд["вердикты"]:
        lines.append("- судов не было")
    lines += ["", "## Худшие промахи недели (|P − исход| максимален)"]
    for m in r["худшие_промахи_7д"]:
        lines.append(f"- {m['актив']} [{m['трек']}] P={m['P']} → исход {m['исход']} "
                     f"({m['направление']}, срок {m['срок']})")
    if not r["худшие_промахи_7д"]:
        lines.append("- разрешённых исходов за неделю нет")
    p = r["промоушен"]
    lines += ["", "## Форвард-промоушен (мост к деньгам)",
              (f"Рёбер с накопленной статистикой: {p.get('n_edges', 0)}; готовы к ярусу A: "
               f"{p.get('n_promote', 0)}." if p.get("есть_отчёт")
               else "Отчёта промоушена ещё нет (cron воскресенье 08:30)."),
              ""]
    ec = r.get("рёбра_кандидаты") or {}
    lines += ["## Добавленные рёбра за неделю (Э4(ж), «перебор мира»)",
              (f"Реестр кандидатов: {ec.get('всего_в_реестре', 0)} всего; за 7 дней: +{ec.get('за_7д', 0)}."
               if ec.get("всего_в_реестре") else
               "Реестр кандидат-рёбер пуст (knowledge/edge_candidates.jsonl — заполняет Э4/Э5).")]
    for e in (ec.get("рёбра_7д") or [])[:20]:
        lines.append(f"- {e['дата']} {e['ребро']} — {e.get('событие')} / {e.get('сегмент')}")
    lines += ["",
              f"## Покрытие датчика «внимание» §R5 (7д): {r['внимание_покрытие_7д']}",
              "",
              "_Автоотчёт детерминированного кода (§25). Глубокий разбор с объяснениями промахов:_",
              "_открой Claude Code → /review-week._"]
    return "\n".join(lines)


def _product_lines(now=None):
    """Аудит 03.11 (P1 №10): пятничная сводка — про ПРОДУКТ (идеи охотника, «Шансы недели», нажатия,
    путь к точке Б) популярным языком; числа — из ops/point_b.py и журнала решений (только чтение)."""
    try:
        sys.path.insert(0, str(ROOT / "ops"))
        import point_b as PB
        import owner_feedback as OF
        m = PB.measure((now or _now()).date().isoformat())
    except Exception as e:                               # noqa: BLE001 — сводка живёт и без замера
        return [f"• замер точки Б не посчитался ({type(e).__name__}) — смотри reports/point_b/"]
    # ре-гейт 03.11: окно — текущая ISO-неделя (пн … сегодня), а не «7 дней назад» (давало «6 из 5»);
    # честный ноль — отдельно от доставленной идеи
    today = (now or _now()).date()
    monday = today - datetime.timedelta(days=today.isoweekday() - 1)
    weekdays = [(monday + datetime.timedelta(days=i)).isoformat() for i in range(min(today.isoweekday(), 5))]
    zero = set(m.get("дни_честного_ноля") or [])
    idea_week = [d for d in m["дни_с_идеей"] if d in weekdays and d not in zero]
    zero_week = [d for d in weekdays if d in zero and d not in idea_week]
    h, t = m["идеи_охотника"], m["событийный_трек"]
    try:
        import yaml as _y
        daily = bool((_y.safe_load((ROOT / "config" / "presentation.yaml").read_text(encoding="utf-8")) or {}).get("daily_hunt"))
    except Exception:                                    # noqa: BLE001
        daily = False
    if daily:
        out = [f"• Идея дня дошла в {len(idea_week)} из {len(weekdays)} будних дней недели"
               + (f"; ещё {len(zero_week)} — честное «сегодня защищать нечего» с причинами." if zero_week else ".")]
    else:
        # 03.11 (решение владельца): охота по запросу — считаем запросы недели (заявки claims/<дата>.txt)
        cdir = ROOT / "reports" / "ideas" / "claims"
        req = sorted(f.stem for f in cdir.glob("*.txt") if f.stem >= monday.isoformat()) if cdir.is_dir() else []
        ideas_w = sorted(d for d in m["дни_с_идеей"] if d >= monday.isoformat() and d not in zero)
        zero_w = sorted(d for d in zero if d >= monday.isoformat())
        out = [(f"• За неделю ты запросил идею {len(req)} раз(а): пришло идей — {len(ideas_w)}, честных «защищать "
                f"нечего» — {len(zero_w)}.") if req else "• За неделю запросов «найди идею» не было."]
    if h["исходов"]:
        out.append(f"• Идеи охотника: сверено {h['исходов']}, сбылось {h['сбылось']}"
                   + (f"; от обычной качки цен сбылось бы около {h['база_ожидание']}" if h["база_ожидание"] is not None else "")
                   + (" — данных мало, вывода об умении пока нет." if h["умение"]["verdict"] == "данных мало"
                      else f" — вердикт: {h['умение']['verdict']}."))
    else:
        out.append(f"• Идеи охотника: записано в счёт {h['печатей']}, сверенных пока нет.")
    out.append(f"• «Шансы недели»: записано вопросов {t['печатей']}, сверено {t['исходов']}.")
    try:
        fb = OF.recent(7)
        if fb:
            cnt = {}
            for x in fb:
                k = x["решение"] + (f" ({x['причина']})" if x.get("причина") else "")
                cnt[k] = cnt.get(k, 0) + 1
            out.append("• Твои нажатия: " + ", ".join(f"{k} — {v}" for k, v in cnt.items()) + ".")
        else:
            out.append("• Твоих нажатий за неделю не было.")
    except Exception:                                    # noqa: BLE001
        pass
    b1 = [x for x in m["критерии"] if x["точка"] == "Б1"]
    out.append("• До точки Б1 (28 ноября): " + "; ".join(f"{x['что']} — {x['сейчас']} из {x['цель']}" for x in b1) + ".")
    return out


def _summary_notice(r):
    суд = r["суд_7д"]
    m = r["треки"]["money"]; pv = r["треки"]["provisional"]; ef = r["треки"]["edge_forward"]
    засуха = r["money_засуха_дней"]
    p = r["промоушен"]
    old_n = m["исходов"] + pv["исходов"] + ef["исходов"]
    week_n = m["исходов_7д"] + pv["исходов_7д"] + (ef.get("исходов_7д") or 0)
    # ре-гейт 03.11 (П8): оценка — из посчитанного Brier, не зашитой фразой
    b = pv.get("brier")
    n_pv = int(pv.get("исходов") or 0)
    if b is None or not n_pv:
        verdict = "счёт точности пока не посчитан"
    else:
        # ре-гейт №2 03.11: сравнение с постоянным прогнозом по СВОЕЙ частоте трека q(1−q), а не с 0.25 —
        # при почти постоянной вероятности абсолютный порог хвалил бы ухудшение
        q = float(pv.get("сбылось") or 0) / n_pv
        base = q * (1 - q)
        verdict = (f"счёт точности {b} при {round(base, 4)} у постоянного прогноза «как обычно» (меньше — "
                   "лучше) — " + ("не лучше такого прогноза, поэтому в идеи дня он не идёт" if float(b) >= base - 0.005
                                  else "чуть лучше, но до доказанного умения далеко"))
    text = (
        "Пятничный разбор: что было за неделю\n\n"
        + "\n".join(_product_lines(r.get("_now"))) + "\n\n"
        f"Старый конвейер работает фоном как измеритель: сверено прогнозов {old_n} (за неделю +{week_n}); "
        f"{verdict}. Связи между бумагами, подтверждённые наблюдением: "
        f"{'есть — нужна твоя подпись' if p.get('n_promote') else 'пока нет'}.\n"
        "Полный отчёт: reports/auto_review/."
    )
    return text


def run_weekly(predictions_path=None, outcomes_path=None, funnel_glob=None,
               promo_path=None, notices_path=None, reports_dir=None, now=None,
               candidates_path=None):
    r = compute_review(predictions_path, outcomes_path, funnel_glob, promo_path, now,
                       candidates_path=candidates_path)
    out_dir = pathlib.Path(reports_dir) if reports_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"review_{r['ts'][:10]}.md"
    md_path.write_text(render_md(r), encoding="utf-8")
    (out_dir / f"review_{r['ts'][:10]}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    _notice(_summary_notice(r), notices_path)
    print(f"[auto_review] еженедельный разбор → {md_path}")
    return r


def _load_state(state_path=None):
    path = pathlib.Path(state_path) if state_path else STATE
    if path.exists():
        try:
            return json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def _save_state(st, state_path=None):
    path = pathlib.Path(state_path) if state_path else STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def _github_push_age_days(now=None, repo_dir=None):
    """Возраст (в днях) последнего СЕРВЕРНОГО чекпоинта журналов в GitHub, или None при недоступности.

    v3/V1 «тишина в GitHub»: с 15.07 по 31.10 пуши молчали 3.5 месяца и никто не заметил.
    Аудит 03.11: меряем не любой коммит (облако коммитит каждый день и маскировало бы тишину
    сервера), а коммит «journal: чекпоинт …» в master ИЛИ в боковой ветке server-journal (туда
    чекпоинт уводит журналы, пока master заморожен). fail-soft: нет сети/не git — честный None."""
    import subprocess
    now = now or _now()
    cwd = str(repo_dir or ROOT)
    try:
        subprocess.run(["git", "fetch", "--quiet", "origin", "master"],
                       cwd=cwd, timeout=60, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "fetch", "--quiet", "origin", "server-journal:refs/remotes/origin/server-journal"],
                       cwd=cwd, timeout=60, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        refs = ["origin/master"]
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", "origin/server-journal"], cwd=cwd,
                          timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            refs.append("origin/server-journal")
        out = subprocess.run(["git", "log", "-1", "--format=%ct", "--grep=^journal: чекпоинт", *refs],
                             cwd=cwd, timeout=30, check=True,
                             capture_output=True, text=True)
        ts = int(out.stdout.strip())
    except Exception:                                    # noqa: BLE001 — сеть/гит недоступны/чекпоинтов нет
        return None
    return (now - datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)).days


def crontab_missing(expected_path=None, installed_text=None):
    """Строки ops/crontab.txt (без комментариев), которых НЕТ в установленном crontab пользователя.

    Аудит 03.11 (датчик V1): 31.10 выяснилось, что чекпоинт-строка жила только в файле — в
    установленный crontab её никто не внёс. installed_text=None → `crontab -l` (fail-soft: None)."""
    import subprocess
    exp_path = pathlib.Path(expected_path or ROOT / "ops" / "crontab.txt")
    try:
        exp = [" ".join(ln.split()) for ln in exp_path.read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return None
    if installed_text is None:
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
        except Exception:                                # noqa: BLE001 — нет crontab в среде
            return None
        if r.returncode != 0:
            return None
        installed_text = r.stdout
    inst = {" ".join(ln.split()) for ln in installed_text.splitlines() if ln.strip()}
    return [ln for ln in exp if ln not in inst]


def deep_dive_waiting(now=None, queue_path=None, days=1):
    """Нажатия «Качаем», которые ждут прокачки дольше `days` суток → [(run_id, asset, с какого числа)]."""
    now = now or _now()
    try:
        from orchestrator import deep_dive as DD
        st = DD.queue_state(queue_path)
    except Exception:                                    # noqa: BLE001 — нет модуля/очереди
        return []
    cut = (now - datetime.timedelta(days=days)).isoformat()
    return [(rid, a, str(v.get("last_ts"))[:10]) for (rid, a), v in sorted(st.items(), key=str)
            if v.get("stage") == "queued" and str(v.get("last_ts") or "") < cut]


def week_odds_missed(now=None, out_dir=None, presentation=None, weekdays=(2, 4)):
    """День прогона «Шансов недели» (вт/чт) при включённом флаге, а нового выпуска за сегодня нет."""
    now = now or _now()
    if now.isoweekday() not in weekdays or now.hour < 9:
        return False
    if presentation is None:
        try:
            import yaml
            presentation = yaml.safe_load((ROOT / "config" / "presentation.yaml").read_text(encoding="utf-8")) or {}
        except Exception:                                # noqa: BLE001
            return False
    if not presentation.get("week_odds_push"):
        return False
    d = pathlib.Path(out_dir or ROOT / "journal" / "week_odds")
    return not any(d.glob(f"wo_{now.strftime('%Y%m%d')}*.json")) if d.exists() else True


def _roadmap_stuck(state_path=None):
    """Этапы карты v3 с stuck_count>=2 → список (этап, название, stuck_count, blocked_on).

    v3/V1 «буксовка»: три июльских пивота умерли молча — теперь брошенный этап обязан шуметь."""
    path = pathlib.Path(state_path) if state_path else ROADMAP_STATE
    if not path.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:                                    # noqa: BLE001 — битый yaml = нет данных
        return []
    stuck = []
    for eid, e in (data.get("этапы") or {}).items():
        if isinstance(e, dict) and int(e.get("stuck_count") or 0) >= 2:
            stuck.append({"этап": eid, "название": e.get("название") or "",
                          "циклов": int(e["stuck_count"]),
                          "вопрос": e.get("blocked_on") or "причина не записана"})
    return stuck


def run_watch(predictions_path=None, outcomes_path=None, funnel_glob=None,
              promo_path=None, notices_path=None, state_path=None, now=None,
              roadmap_path=None, push_age_days="auto", repo_dir=None):
    """Ежедневный дозор: алерт только при срабатывании и один раз на состояние (дедуп)."""
    now = now or _now()
    st = _load_state(state_path)
    fired = []

    preds, _outs = _load(predictions_path, outcomes_path)
    засуха = _days_since_money_seal(preds, now)
    # 1) засуха денежной печати: алерт на 7-й день и далее каждые 7 (7, 14, 21…)
    if засуха is not None and засуха >= DROUGHT_ALERT_DAYS:
        step = засуха // DROUGHT_ALERT_DAYS
        if st.get("drought_step") != step:
            fired.append(_notice(
                f"🩺 Дозор §25: {засуха} дн. подряд ни одной ДЕНЕЖНОЙ печати (§11-трек). "
                f"Идеи в money-кандидаты попадают, но слепой суд их валит — счёт к воротам-270 "
                f"стоит. Это ждёт форвард-промоушена (B4) либо решения по рубрике (слой Б, "
                f"подпись). Детали: еженедельный авторазбор в reports/auto_review/.",
                notices_path, silent=True))
            st["drought_step"] = step
    else:
        st.pop("drought_step", None)

    # 2) суд валит 100% при достаточной выборке за неделю
    суд = _court_week(funnel_glob, now=now)
    if суд["судов"] >= COURT_STREAK_MIN and суд["разбито"] == суд["судов"]:
        key = f"{суд['судов']}/{суд['разбито']}@{now.strftime('%G-%V')}"   # раз в ISO-неделю
        if st.get("court_alert_key") != key:
            fired.append(_notice(
                f"🩺 Дозор §25: слепой суд за 7 дней разбил {суд['разбито']}/{суд['судов']} "
                f"денежных кандидатов (100%). Либо кандидаты системно слабые (r²≈0, ход ниже "
                f"шума), либо планка/данные суда несправедливы к ним — тема для /review-week.",
                notices_path, silent=True))
            st["court_alert_key"] = key

    # 3) корректировка доступна: свежий отчёт промоушена с рёбрами, прошедшими §10-гейт
    p = _promotions_status(promo_path)
    if p.get("n_promote"):
        mt = p.get("mtime")
        if st.get("promo_alerted_mtime") != mt:
            рёбра = ", ".join(p.get("рёбра", [])[:5])
            fired.append(_notice(
                f"✅ Дозор §25: КОРРЕКТИРОВКА ДОСТУПНА — {p['n_promote']} ребро(а) прошли "
                f"форвард-гейт §10 (N≥30, значимость): {рёбра}. Применение — только твоей "
                f"подписью: sudo -u oracle .venv/bin/python ops/promote_edges.py --apply "
                f"(авто-apply удержан: открыт долг B4(а) псевдорепликации корма).",
                notices_path))
            st["promo_alerted_mtime"] = mt

    # 4) v3/V1: тишина в GitHub — последний коммит origin/master старше PUSH_AGE_ALERT_DAYS.
    #    Дедуп ступенями (4, 8, 12… дней), как засуха. push_age_days: "auto" = померить git'ом;
    #    число/None — для тестов и офлайна.
    age = _github_push_age_days(now, repo_dir) if push_age_days == "auto" else push_age_days
    if age is not None and age >= PUSH_AGE_ALERT_DAYS:
        step = age // PUSH_AGE_ALERT_DAYS
        if st.get("push_age_step") != step:
            fired.append(_notice(
                f"🩺 Дозор: журналы сервера не доезжали до GitHub {age} дн. — вечерний чекпоинт "
                f"(cron 22:00) не прошёл ни в главную, ни в запасную ветку. Летом так молча пропали "
                f"3.5 месяца. Проверить: sudo crontab -u oracle -l | grep checkpoint и хвост journal/cron.log.",
                notices_path))
            st["push_age_step"] = step
    elif age is not None:
        st.pop("push_age_step", None)

    # 5) v3/V1: буксовка этапа дорожной карты — stuck_count>=2 в ops/roadmap_state.yaml →
    #    вопрос-развилка владельцу (дедуп по (этап, счётчик): рост счётчика шумит снова).
    for sk in _roadmap_stuck(roadmap_path):
        key = f"{sk['этап']}@{sk['циклов']}"
        seen = st.setdefault("roadmap_stuck_alerted", [])
        if key not in seen:
            fired.append(_notice(
                f"🧭 Дорожная карта v3: этап {sk['этап']} «{sk['название']}» буксует "
                f"{sk['циклов']} цикла подряд. Нужно твоё слово: {sk['вопрос']} — "
                f"ответь в чате или боту, иначе этап стоит (правило против тихого бросания).",
                notices_path))
            seen.append(key)
            st["roadmap_stuck_alerted"] = seen[-20:]

    # 6) аудит 03.11 (V1): установленный crontab разошёлся с ops/crontab.txt
    miss = crontab_missing() if push_age_days == "auto" else None
    if miss:
        key = str(sorted(miss))[:500]
        if st.get("crontab_missing_key") != key:
            fired.append(_notice(
                f"🩺 Дозор: в установленном расписании сервера нет {len(miss)} строк из ops/crontab.txt "
                f"(например: «{miss[0][:120]}»). Пока их нет, эти задачи не запускаются. Одна команда: "
                f"sudo crontab -u oracle /home/oracle/oracle/ops/crontab.txt", notices_path))
            st["crontab_missing_key"] = key
    elif miss is not None:
        st.pop("crontab_missing_key", None)

    # 7) аудит 03.11 (V1): «Качаем» ждёт прокачки дольше суток (раз на нажатие)
    seen_dd = st.setdefault("deep_dive_waiting_alerted", [])
    for rid, asset, since in (deep_dive_waiting(now) if push_age_days == "auto" else []):
        if rid in seen_dd:
            continue
        fired.append(_notice(
            f"🩺 Дозор: «Качаем» по {asset} (нажато {since}) ждёт прокачки дольше суток. Двухдневная "
            f"прокачка на сервере пока выключена до твоего «ок» после ручного показа: "
            f"cd /home/oracle/oracle && sudo -u oracle sh -c 'set -a && . ./.env && set +a && "
            f".venv/bin/python orchestrator/deep_dive.py --show'", notices_path))
        seen_dd.append(rid)
        st["deep_dive_waiting_alerted"] = seen_dd[-30:]

    # 8) аудит 03.11 (V1): день «Шансов недели» без нового выпуска при включённом флаге
    if push_age_days == "auto" and week_odds_missed(now):
        key = now.date().isoformat()
        if st.get("week_odds_missed_day") != key:
            fired.append(_notice(
                "🩺 Дозор: сегодня день «Шансов недели», флаг включён, а нового выпуска нет — "
                "прогон не запускался или упал. Хвост: journal/cron.log (метка week_odds).", notices_path))
            st["week_odds_missed_day"] = key

    _save_state(st, state_path)
    print(f"[auto_review --watch] сработавших сигналов: {len(fired)}"
          + (f" · money-засуха {засуха} дн." if засуха is not None else ""))
    return {"fired": [f["text"][:80] for f in fired], "засуха_дней": засуха,
            "суд_7д": {"судов": суд["судов"], "разбито": суд["разбито"]},
            "промоушен_n": p.get("n_promote", 0)}


def main():
    ap = argparse.ArgumentParser(description="Автопетля §25: еженедельный разбор / ежедневный дозор")
    ap.add_argument("--watch", action="store_true", help="ежедневный дозор (алерты только при срабатывании)")
    a = ap.parse_args()
    if a.watch:
        run_watch()
    else:
        run_weekly()
    return 0


if __name__ == "__main__":
    sys.exit(main())
