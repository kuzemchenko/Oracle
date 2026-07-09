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

WEEK_DAYS = 7
DROUGHT_ALERT_DAYS = 7          # засуха money-печати: первый алерт на 7-й день, далее раз в 7
COURT_STREAK_MIN = 4            # 100% РАЗБИТА при ≥N судах за неделю — это уже закономерность


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _notice(text, notices_path=None):
    """Заметка владельцу: строка в journal/notices.jsonl — бот пушит новые по курсору
    (тот же канал доставки, что alerts: доставка/ретраи на стороне бота)."""
    path = pathlib.Path(notices_path) if notices_path else NOTICES
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now().isoformat(timespec="seconds"), "text": text}
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


def compute_review(predictions_path=None, outcomes_path=None, funnel_glob=None,
                   promo_path=None, now=None):
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
              "",
              f"## Покрытие датчика «внимание» §R5 (7д): {r['внимание_покрытие_7д']}",
              "",
              "_Автоотчёт детерминированного кода (§25). Глубокий разбор с объяснениями промахов:_",
              "_открой Claude Code → /review-week._"]
    return "\n".join(lines)


def _summary_notice(r):
    суд = r["суд_7д"]
    m = r["треки"]["money"]; pv = r["треки"]["provisional"]; ef = r["треки"]["edge_forward"]
    засуха = r["money_засуха_дней"]
    p = r["промоушен"]
    text = (
        "📋 Еженедельный авторазбор §25 (0.25 = уровень «монетки», меньше — лучше)\n"
        f"• денежный трек: исходов {m['исходов']} (за 7д +{m['исходов_7д']}), Brier {m['brier'] or '—'}"
        + (f"; ⚠ {засуха} дн. без новой денежной печати" if засуха is not None and засуха >= DROUGHT_ALERT_DAYS else "") + "\n"
        f"• провизорный: исходов {pv['исходов']} (за 7д +{pv['исходов_7д']}), Brier {pv['brier'] or '—'}\n"
        f"• B4-рёбра: печатей {ef['запечатано']}, исходов {ef['исходов']}, Brier {ef['brier'] or '—'}\n"
        f"• слепой суд за 7д: {суд['судов']} судов, разбито {суд['разбито']}\n"
        f"• корректировка (промоушен рёбер): {'ДОСТУПНА — жду подписи (ops/promote_edges.py --apply)' if p.get('n_promote') else 'пока недоступна (нет рёбер, прошедших §10-гейт)'}\n"
        "Полный отчёт: reports/auto_review/. Глубокий разбор: /review-week в Claude Code."
    )
    return text


def run_weekly(predictions_path=None, outcomes_path=None, funnel_glob=None,
               promo_path=None, notices_path=None, reports_dir=None, now=None):
    r = compute_review(predictions_path, outcomes_path, funnel_glob, promo_path, now)
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


def run_watch(predictions_path=None, outcomes_path=None, funnel_glob=None,
              promo_path=None, notices_path=None, state_path=None, now=None):
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
                notices_path))
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
                notices_path))
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
