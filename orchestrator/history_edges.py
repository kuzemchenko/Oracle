# -*- coding: utf-8 -*-
"""orchestrator/history_edges.py — библиотека ИСТОРИЧЕСКИХ КАСКАДОВ → пары под форвард-счёт (V6).

Просьба владельца 03.11: «возьми из мировой истории сильные движения, почему они начинались, волны
второго порядка, эффекты бабочки — набей примеры и ищи подобное». Контур:
  1. build — эпизоды от воркфлоу cascade-history (сборщики по доменам → скептики-проверяющие:
     источник открыт, дата сверена, тикер торговался) → валидация КОДОМ → к каждому звену КОД
     дописывает настоящий ход цены после повода (5/20/60 торговых дней, в долях и в «сигмах» своей
     обычной качки) → knowledge/cascade_history.yaml. Модель чисел не пишет (П8/Инв#6).
  2. pairs — пары «звено → следующее звено» на СЕГОДНЯШНИХ бумагах той же роли; КОД проходит всю
     доступную историю (Yahoo с 2001, индексы раньше), находит все резкие движения первого звена
     в сторону эпизода и считает, как часто второе шло следом сильнее своей обычной качки — против
     БАЗЫ (mathlib/event_study.follow_stats); поправка на множественность — Бенджамини–Хохберг
     (mathlib/fdr). Горизонт объявлен заранее из оценки лага звена (без подгонки).
     --append: пары, которые история НЕ опровергла и у которых знак механизма не спорит со знаком
     корреляции (форвард-тест B4 ставит по β), — в knowledge/edge_candidates.jsonl с origin=history
     и провенансом (эпизоды, источники, счёт истории). Дальше — ежедневный форвард-тест B4.
П16: история — только ОТСЕВ («история против» не идёт в форвард), не доказательство: эпизоды отобраны
моделью, знающей исход. Умение пара зарабатывает ТОЛЬКО форвардом. Журналы честности не трогает.
"""
import argparse
import collections
import datetime
import glob
import hashlib
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import event_study as ES      # noqa: E402
from mathlib import fdr as FDR              # noqa: E402

LIBRARY = ROOT / "knowledge" / "cascade_history.yaml"
MECHS = ROOT / "knowledge" / "mechanisms.yaml"
SNAPSHOT = ROOT / "reports" / "prices" / "closes_latest.json"
REPORTS = ROOT / "reports" / "history"
ORIGIN = "history"
LAG_DAYS = 0          # форвард-тест B4 меряет догоняющий ход на лаге 0 (как тезисы/перебор мира)
Q_FDR = 0.10
MAX_PAIRS_PER_EPISODE = 8
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYM = re.compile(r"^(FRED:[A-Z0-9_]+|\^?[A-Z0-9][A-Z0-9.\-]{0,9}(=F)?)$")
APPEND_VERDICTS = ("подтверждена", "намёк", "мало данных")
RHO_CONFLICT = 0.05   # знак корреляции «спорит» с механизмом, только если |ρ| заметен (около нуля — нет спора)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _sym(s):
    s = str(s or "").strip()
    if not s or s.lower() in ("нет", "none", "n/a", "-"):
        return None
    if s.upper().startswith("FRED:"):
        s = "FRED:" + s[5:].strip().upper()
    else:
        s = s.upper()
        if s.endswith(".US"):
            s = s[:-3]
    return s if _SYM.match(s) else None


def server_symbols(path=SNAPSHOT):
    """Бумаги серверной базы (их сервер форвард-тестит): {'FRO', ...}."""
    try:
        snap = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {k[:-3] for k in (snap.get("symbols") or {}) if k.endswith(".US")}


def mechanism_ids(path=MECHS):
    try:
        doc = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    except OSError:
        return set()
    return {m.get("id") for m in doc.get("механизмы") or [] if m.get("id")}


# ── 1. build ─────────────────────────────────────────────────────────────────────────────────────
def validate_episode(e, today, mechs=None):
    """→ (эпизод_нормализованный | None, [причины отказа]). Проверка формы КОДОМ (П8)."""
    why = []
    if not isinstance(e, dict):
        return None, ["не объект"]
    eid = str(e.get("id") or "").strip()
    d = str(e.get("trigger_date") or "")
    if not eid:
        why.append("нет id")
    if not _DATE.match(d):
        why.append(f"дата повода не ГГГГ-ММ-ДД: {d!r}")
    else:
        try:
            if datetime.date.fromisoformat(d) > today:
                why.append("дата повода в будущем")
        except ValueError:
            why.append(f"несуществующая дата {d}")
    src = [s for s in e.get("sources") or [] if isinstance(s, dict) and str(s.get("url", "")).startswith("http")]
    if not src:
        why.append("нет источника-URL")
    ver = e.get("verified") or {}
    if ver and ver.get("source_ok") is False:
        why.append("скептик: источник не подтвердил событие")
    links = []
    for L in e.get("links") or []:
        if not isinstance(L, dict):
            continue
        if L.get("direction") not in ("up", "down"):
            continue
        try:
            order = int(L.get("order"))
        except (TypeError, ValueError):
            continue
        links.append({**L, "order": order, "then_symbol": _sym(L.get("then_symbol")),
                      "today_symbol": _sym(L.get("today_symbol")),
                      "lag_days_guess": max(0, int(L.get("lag_days_guess") or 0))})
    links.sort(key=lambda x: (x["order"], str(x.get("role_tag"))))
    if sum(1 for L in links if L["then_symbol"]) < 2:
        why.append("меньше двух звеньев с торгуемым тикером того времени")
    if not any(L["order"] == 0 for L in links):
        why.append("нет звена order=0 (сам шок)")
    if why:
        return None, why
    mech = str(e.get("mechanism_id") or "")
    out = {k: e.get(k) for k in ("id", "trigger_date", "date_precision", "title_ru", "what_ru", "domain",
                                 "mechanism_id", "shock_type", "resources", "region", "counter_case_ru",
                                 "analog_rule_ru", "domain_key", "merged_from")}
    out.update({"id": eid, "sources": src, "links": links,
                "mechanism_known": bool(mechs is None or mech in mechs or mech.startswith("new:")),
                "verified": ver or None})
    return out, []


_BROAD = {"SPY", "^GSPC", "^VIX", "^TNX", "^DJI", "^IXIC", "QQQ", "GC=F", "GLD", "TLT", "DX-Y.NYB", "UUP"}


def merge_duplicates(eps, days=14):
    """Один и тот же эпизод от разных сборщиков (например, 24.02.2022 в «санкциях», «газе» и «обороне»):
    даты ближе `days` и общий специфичный тикер того времени (широкие индексы/золото не в счёт) —
    оставляем эпизод с бОльшим числом звеньев, остальные id — в merged_from. → (уникальные, склеено)."""
    out, merged = [], 0
    for e in sorted(eps, key=lambda x: (-len(x.get("links") or []), x["trigger_date"], x["id"])):
        syms = {L["then_symbol"] for L in e["links"] if L.get("then_symbol")} - _BROAD
        d = datetime.date.fromisoformat(e["trigger_date"])
        twin = None
        for o in out:
            if abs((datetime.date.fromisoformat(o["trigger_date"]) - d).days) <= days and \
                    syms & ({L["then_symbol"] for L in o["links"] if L.get("then_symbol")} - _BROAD):
                twin = o
                break
        if twin is None:
            out.append(e)
            continue
        merged += 1
        twin["merged_from"] = sorted(set((twin.get("merged_from") or []) + [e["id"]] + (e.get("merged_from") or [])))
    return out, merged


def load_inputs(patterns):
    eps = []
    for pat in patterns:
        for f in sorted(glob.glob(pat)):
            try:
                data = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                data = data.get("episodes") or []
            for e in data if isinstance(data, list) else []:
                if isinstance(e, dict):
                    eps.append({**e, "_file": pathlib.Path(f).name})
    return eps


def _round(x, n=4):
    return None if x is None else round(float(x), n)


def price_link(L, trigger_date, series_fn):
    """Код дописывает к звену настоящий ход цены после повода (по тикеру того времени)."""
    sym = L.get("then_symbol")
    if not sym:
        return {"есть": False, "причина": "тикер того времени не указан — звено рассказ без цифр"}
    s = series_fn(sym)
    if not s:
        return {"есть": False, "причина": f"нет истории {sym} в Yahoo/FRED"}
    m = ES.episode_move(s[0], s[1], trigger_date)
    if not m or not m["ходы"].get(5):
        return {"есть": False, "причина": f"история {sym} не покрывает дату повода"}
    want = 1 if L["direction"] == "up" else -1
    out = {"есть": True, "тикер": sym, "база_дата": m["база_дата"], "σ_дн": _round(m["σ_дн"], 5)}
    for h in (5, 20, 60):
        x = m["ходы"].get(h)
        out[f"ход_{h}"] = _round(x["ход"]) if x else None
        out[f"z_{h}"] = _round(x["z"], 2) if x and x["z"] is not None else None
    h = ES.horizon_bucket(L.get("lag_days_guess"))
    xh = m["ходы"].get(h) or m["ходы"].get(5)
    out["горизонт_проверки"] = h
    out["направление_совпало"] = bool(xh and xh["ход"] * want > 0)
    return out


def build(patterns, *, out=LIBRARY, series_fn=None, today=None, mechs=None, now_dt=None):
    """Эпизоды воркфлоу → валидация → цены звеньев кодом → knowledge/cascade_history.yaml."""
    if series_fn is None:
        from ops import history_prices as HP
        series_fn = HP.series
    today = today or _now().date()
    mechs = mechanism_ids() if mechs is None else mechs
    raw = load_inputs(patterns)
    kept, rejected, ids = [], [], set()
    for e in raw:
        ep, why = validate_episode(e, today, mechs)
        if not ep:
            rejected.append({"id": e.get("id"), "файл": e.get("_file"), "причины": why})
            continue
        if ep["id"] in ids:
            rejected.append({"id": ep["id"], "файл": e.get("_file"), "причины": ["дубль id"]})
            continue
        ids.add(ep["id"])
        kept.append(ep)
    kept, n_merged = merge_duplicates(kept)
    for ep in kept:
        for L in ep["links"]:
            L["prices"] = price_link(L, ep["trigger_date"], series_fn)
        priced = [L for L in ep["links"] if L["prices"].get("есть")]
        ep["звеньев_с_ценой"] = len(priced)
        ep["направление_совпало"] = sum(1 for L in priced if L["prices"].get("направление_совпало"))
    kept.sort(key=lambda x: (x["trigger_date"], x["id"]))
    doc = {"версия": 1,
           "создано": (now_dt or _now()).isoformat(timespec="seconds"),
           "что_это": ("Библиотека исторических каскадов: повод → прямая волна → волны 2–3 порядка → неочевидная "
                       "бумага; провальный аналог и правило переноса. Рассказ и тикеры — от сборщиков и скептиков "
                       "воркфлоу cascade-history (с источниками); ЧИСЛА (поле prices) — только код: "
                       "ops/history_prices (Yahoo/FRED, скорректированные закрытия) + mathlib/event_study."),
           "как_читать_prices": ("ход_h — логдоходность от закрытия ДО повода через h торговых дней; z_h — тот же ход "
                                 "в сигмах обычной качки бумаги за год до повода; направление_совпало — пошла ли "
                                 "бумага туда, куда сказал рассказ, на объявленном горизонте."),
           "честность": ("П16: эпизоды отобраны моделью, знающей исход, — это память аналогий и отсев, не "
                         "доказательство умения. Умение пар меряет только форвард-тест."),
           "эпизодов": len(kept), "отклонено_кодом": len(rejected), "склеено_дублей": n_merged,
           "эпизоды": kept}
    p = pathlib.Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    tmp.replace(p)
    return {"на_входе": len(raw), "эпизодов": len(kept), "склеено_дублей": n_merged, "отклонено": rejected,
            "звеньев": sum(len(e["links"]) for e in kept),
            "звеньев_с_ценой": sum(e["звеньев_с_ценой"] for e in kept),
            "направление_совпало": sum(e["направление_совпало"] for e in kept)}


def load_library(path=LIBRARY):
    try:
        return (yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}).get("эпизоды") or []
    except OSError:
        return []


# ── 2. pairs ─────────────────────────────────────────────────────────────────────────────────────
def episode_pairs(ep):
    """Пары внутри эпизода на СЕГОДНЯШНИХ бумагах: каждое звено порядка k ← звенья порядка k−1, плюс
    сам шок (order 0) → звенья порядка ≥2. Знак = совпадение направлений; горизонт — из разницы лагов."""
    links = [L for L in ep.get("links") or [] if L.get("today_symbol")]
    by_order = collections.defaultdict(list)
    for L in links:
        by_order[int(L["order"])].append(L)
    out = []
    for L in links:
        k = int(L["order"])
        parents = list(by_order.get(k - 1, [])) if k >= 1 else []
        if k >= 2:
            parents += [P for P in by_order.get(0, []) if P not in parents]
        for P in parents:
            a, b = P["today_symbol"], L["today_symbol"]
            if a == b:
                continue
            out.append({"from": a, "to": b,
                        "src_dir": 1 if P["direction"] == "up" else -1,
                        "sign": 1 if P["direction"] == L["direction"] else -1,
                        "horizon": ES.horizon_bucket(int(L.get("lag_days_guess") or 0)
                                                     - int(P.get("lag_days_guess") or 0)),
                        "episode": ep["id"], "mechanism": ep.get("mechanism_id"),
                        "why": L.get("why_ru"), "role_from": P.get("role_tag"), "role_to": L.get("role_tag"),
                        "non_obvious": bool(L.get("non_obvious")),
                        "source": (ep.get("sources") or [{}])[0].get("url")})
    return out[:MAX_PAIRS_PER_EPISODE]


def test_pairs(episodes, *, series_fn=None, server=None):
    """Все пары библиотеки → счёт истории кодом. Пара = (from, to, направление шока); знаки из разных
    эпизодов сводятся; конфликт знаков — честно «конфликт», без проверки."""
    if series_fn is None:
        from ops import history_prices as HP
        series_fn = HP.series
    server = server_symbols() if server is None else server
    groups = collections.OrderedDict()
    for ep in episodes:
        for pr in episode_pairs(ep):
            key = (pr["from"], pr["to"], pr["src_dir"])
            groups.setdefault(key, []).append(pr)
    results = []
    for (a, b, sd), prs in groups.items():
        signs = {p["sign"] for p in prs}
        hz = collections.Counter(p["horizon"] for p in prs)
        horizon = sorted(hz.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rec = {"from": a, "to": b, "шоки_источника": "вверх" if sd > 0 else "вниз",
               "src_dir": sd, "знак": sorted(signs)[0] if len(signs) == 1 else None,
               "горизонт": horizon, "эпизоды": sorted({p["episode"] for p in prs}),
               "механизмы": sorted({str(p["mechanism"]) for p in prs}),
               "роль_from": prs[0]["role_from"], "роль_to": prs[0]["role_to"],
               "почему": prs[0]["why"], "неочевидно": any(p["non_obvious"] for p in prs),
               "источники": sorted({p["source"] for p in prs if p.get("source")}),
               "на_сервере": a in server and b in server}
        if len(signs) > 1:
            rec.update({"вердикт": "конфликт знаков", "причина": "эпизоды дают противоположные знаки"})
            results.append(rec)
            continue
        sa, sb = series_fn(a), series_fn(b)
        if not sa or not sb:
            rec.update({"вердикт": "нет истории", "причина": f"нет цен {a if not sa else b}"})
            results.append(rec)
            continue
        st = ES.follow_stats(sa, sb, rec["знак"], horizon=horizon, src_dir=sd)
        cs, rho, n = ES.corr_sign(sa, sb)
        st.pop("события", None)
        rec.update({"счёт": st, "вердикт": st["вердикт"], "ρ": rho, "знак_корреляции": cs})
        results.append(rec)
    ps = [(i, r["счёт"]["p"]) for i, r in enumerate(results) if (r.get("счёт") or {}).get("p") is not None]
    if ps:
        bh = FDR.benjamini_hochberg([p for _i, p in ps], q=Q_FDR)
        for (i, _p), q, rej in zip(ps, bh["qvalues"], bh["rejected"]):
            results[i]["q"] = round(float(q), 4)
            if results[i]["вердикт"] == "подтверждена" and not rej:
                results[i]["вердикт"] = "намёк"
                results[i]["причина"] = f"p≤0.05, но после поправки на множественность q={round(float(q), 3)}>{Q_FDR}"
    return results


def eligible(r):
    """В форвард-тест: история не опровергла, знак механизма не спорит с корреляцией (B4 ставит по β),
    обе бумаги в серверной базе."""
    if r.get("вердикт") not in APPEND_VERDICTS:
        return False, f"вердикт истории «{r.get('вердикт')}»"
    if not r.get("на_сервере"):
        return False, "бумаги нет в серверной базе — форвард-тест её не видит"
    if r.get("знак_корреляции") and r.get("знак") and r["знак_корреляции"] != r["знак"] \
            and abs(r.get("ρ") or 0) >= RHO_CONFLICT:
        return False, (f"знак механизма {'+' if r['знак'] > 0 else '−'} против корреляции ρ={r.get('ρ')} — "
                       "форвард-тест проверял бы корреляцию, а не механизм")
    return True, "ок"


def append_candidates(results, *, registry=None, sens_path=None, now_dt=None):
    """Пары, прошедшие eligible, → knowledge/edge_candidates.jsonl (origin=history), идемпотентно."""
    from orchestrator import edge_forward as EFW
    from mathlib.calibration import forward_promotion as FP
    reg = pathlib.Path(registry) if registry else EFW.CANDIDATES
    lib_keys = {FP.edge_key(e["from"], e["to"], e["lag"])
                for e in EFW.edge_library(sens_path or EFW.SENS, candidates_path=None)}
    have = set()
    if reg.exists():
        for line in reg.read_text(encoding="utf-8").splitlines():
            try:
                have.add(json.loads(line).get("edge_key"))
            except (ValueError, AttributeError):
                continue
    now_iso = (now_dt or _now()).isoformat(timespec="seconds")
    added, skipped = [], collections.Counter()
    reg.parent.mkdir(parents=True, exist_ok=True)
    with open(reg, "a", encoding="utf-8") as f:
        for r in results:
            ok, why = eligible(r)
            if not ok:
                skipped[why.split(" — ")[0]] += 1
                continue
            key = FP.edge_key(r["from"] + ".US", r["to"] + ".US", LAG_DAYS)
            if key in lib_keys or key in have:
                skipped["уже под наблюдением"] += 1
                continue
            st = r.get("счёт") or {}
            rec = {"ts": now_iso, "edge_key": key, "origin": ORIGIN,
                   "from": r["from"] + ".US", "to": r["to"] + ".US", "lag": LAG_DAYS,
                   "механизм": r["механизмы"][0] if r.get("механизмы") else None,
                   "эпизоды": r["эпизоды"], "шоки_источника": r["шоки_источника"],
                   "знак_механизма": r["знак"], "причинность": r.get("почему"),
                   "роли": f"{r.get('роль_from')} → {r.get('роль_to')}",
                   "источники": r.get("источники")[:3],
                   "история": {"вердикт": r["вердикт"], "горизонт": st.get("горизонт"),
                               "событий": st.get("событий"), "лет": st.get("лет"), "пошло": st.get("пошло"),
                               "доля": st.get("доля"), "база": st.get("база"), "p": st.get("p"),
                               "q": r.get("q"), "ρ": r.get("ρ"), "с": st.get("с")},
                   "провенанс": ("библиотека исторических каскадов knowledge/cascade_history.yaml (воркфлоу "
                                 "cascade-history 03.11, эпизоды с источниками); счёт истории — код "
                                 "mathlib/event_study по Yahoo/FRED (отсев, не доказательство — П16); "
                                 "лаг 0 — форвард-тест B4 меряет догоняющий ход")}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            have.add(key)
            added.append(f"{r['from']}→{r['to']}")
    return {"добавлено": added, "пропущено": dict(skipped)}


def render_md(results, lib_summary=None, today=None):
    cnt = collections.Counter(r["вердикт"] for r in results)
    el = [r for r in results if eligible(r)[0]]
    L = [f"# Исторические каскады: счёт пар — {today or _now().date().isoformat()}", "",
         "Что это значит для продукта: из истории взяты эпизоды сильных движений с волнами второго-третьего "
         "порядка. Для каждой пары «звено → следующее звено» код прошёл ВСЮ историю цен, нашёл все резкие "
         "движения первого звена в ту же сторону, что в эпизоде, и посчитал, как часто второе звено потом "
         "шло следом сильнее своей обычной качки, — и сравнил с тем, как часто оно ходит так само по себе. "
         "Пары, которые история опровергла, в форвард не идут; остальные ставятся под счёт вперёд.", "",
         "Вердикты: " + ", ".join(f"{k} — {v}" for k, v in cnt.most_common()), "",
         f"В форвард-тест годны: {len(el)}", "",
         "| Пара | Шоки | Знак | Горизонт | Событий/лет | Пошло | База | p | q | Вердикт | Эпизоды |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    order = {"подтверждена": 0, "намёк": 1, "мало данных": 2, "история против": 3}
    for r in sorted(results, key=lambda r: (order.get(r["вердикт"], 9), r["from"], r["to"])):
        st = r.get("счёт") or {}
        L.append(f"| {r['from']}→{r['to']} | {r['шоки_источника']} | "
                 f"{'+' if r.get('знак') == 1 else '−' if r.get('знак') == -1 else '?'} | {r.get('горизонт')} | "
                 f"{st.get('событий', '—')}/{st.get('лет', '—')} | {st.get('доля', '—')} | {st.get('база', '—')} | "
                 f"{st.get('p', '—')} | {r.get('q', '—')} | {r['вердикт']} | {', '.join(r['эпизоды'][:3])} |")
    L += ["", "Это исследовательский инструмент, не инвестиционная рекомендация."]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="исторические каскады: библиотека и пары под форвард-счёт")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="эпизоды воркфлоу → knowledge/cascade_history.yaml (цены — код)")
    b.add_argument("inputs", nargs="+", help="файлы/шаблоны ver_*.json")
    p = sub.add_parser("pairs", help="счёт пар по истории → reports/history/")
    p.add_argument("--append", action="store_true", help="годные пары → knowledge/edge_candidates.jsonl")
    p.add_argument("--today")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        s = build(a.inputs)
        print(json.dumps({k: (v if k != "отклонено" else len(v)) for k, v in s.items()}, ensure_ascii=False))
        for r in s["отклонено"][:40]:
            print(" отклонён:", r["id"], "—", "; ".join(r["причины"]))
        return 0
    today = a.today or _now().date().isoformat()
    eps = load_library()
    res = test_pairs(eps)
    REPORTS.mkdir(parents=True, exist_ok=True)
    md = render_md(res, today=today)
    (REPORTS / f"pairs_{today}.md").write_text(md, encoding="utf-8")
    (REPORTS / f"pairs_{today}.json").write_text(
        json.dumps({"дата": today, "пары": res,
                    "код_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(md[:3000])
    if a.append:
        print(json.dumps(append_candidates(res), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
