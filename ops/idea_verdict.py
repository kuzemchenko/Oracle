# -*- coding: utf-8 -*-
"""ops/idea_verdict.py — «ИНВЕСТИЦИОННЫЙ КОМИТЕТ»: финальный вердикт по идее (Q-вердикт-комитет, владелец 22.11).

ПОЧЕМУ: «идей ворох, не вижу, какая заслуживает внимания и на какую можно сделать ставочку»; проработка
кончалась «с одной стороны — с другой стороны», а поиск — «паузой». Теперь у каждой проработанной идеи один
итог: балл «X из 10» по фиксированному листу (knowledge/idea_committee.yaml) и одно из трёх решений —
кандидат на маленькую ставку / лист ожидания с условием / архив с причиной.

КАК: балл ставит КОД. Пункт зачтён, только если в карточке охотника или в данных гипотезы поиска есть
положительное доказательство — факт со ссылкой или число, посчитанное кодом (те же проверки, что в
ops/evidence_check.py и ops/idea_gate.py; «не измерено» = не зачтено). Модель собирает доказательства и не
может поставить балл. Сектор (п.7) — ops/sector_view.py. Балл — НЕ вероятность роста; зарабатывают ли идеи
с высоким баллом чаще — показывает счёт по группам балла (track).

  python3 ops/idea_verdict.py card <карточка.card.json> [--today YYYY-MM-DD] [--court PASS|FIX|FATAL] [--save]
  python3 ops/idea_verdict.py search <id поиска | latest> [--save]
  python3 ops/idea_verdict.py board            → сводка: все кандидаты по баллу, сверху — заслуживающие внимания
  python3 ops/idea_verdict.py track            → счёт метода по группам балла (из сохранённых вердиктов)
"""
import argparse
import datetime
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from mathlib import idea_score as IS            # noqa: E402
from mathlib import materiality as MAT          # noqa: E402
from mathlib import priced_in as PI             # noqa: E402
from mathlib import target_calc as TC           # noqa: E402
import evidence_check as EV                     # noqa: E402
import idea_gate as G                           # noqa: E402
import idea_rules as IR                         # noqa: E402

RULES = ROOT / "knowledge" / "idea_committee.yaml"
OUT = ROOT / "reports" / "ideas" / "verdicts"
INDEX = "verdicts.jsonl"
MIN_N_TRACK = 30
DISCLAIMER = "Балл — не вероятность роста: это число проверок, пройденных с доказательством."


def load_rules(path=None):
    return yaml.safe_load(pathlib.Path(path or RULES).read_text(encoding="utf-8")) or {}


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _http(u):
    return str(u or "").startswith("http")


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _pt(ok, why):
    return {"зачтён": ok, "почему": why}


# ── общие проверки (одни и те же для карточки охотника и гипотезы поиска) ──────────────────────────
def _p_chain(chain):
    fails, labels = [], []
    EV.check_chain({"chain": chain}, fails, labels)
    if fails:
        return _pt(False, fails[0].replace("цепочка: ", ""))
    if labels:
        return _pt(False, "одно звено от события до компании — гипотеза без источника")
    return _pt(True, f"все звенья ({len(chain)}) подтверждены источником")


def _p_materiality(mt):
    fails, labels = [], []
    r = EV.check_materiality({"materiality": mt}, fails, labels, required=True)
    if not r or not r.get("ok"):
        if mt:
            return _pt(False, "размер эффекта записан словами, без чисел для пересчёта кодом — нужны "
                              "effect_profit_per_year (или effect_revenue_per_year), net_income (или revenue), market_cap")
        return _pt(None, "эффект для прибыли компании не посчитан")
    if r["вердикт"] != MAT.СУЩЕСТВЕННО:
        return _pt(False, "эффект посчитан, но мал для размера компании (" + str(r["вердикт"]).lower() + ")")
    return _pt(True, "эффект посчитан и существенен для размера компании")


def _p_durability(d):
    labels = []
    EV.check_durability({"durability": d}, [], labels)
    if labels:              # данных нет вовсе → «не измерено» (None), а не «проверено и не прошло» — для счёта строгости
        return _pt(None if not d else False, "не показано фактом со ссылкой, почему эффект продержится")
    return _pt(True, "устойчивость эффекта подкреплена фактом со ссылкой")


def _p_market(mv):
    if not isinstance(mv, dict) or not mv:
        return _pt(None, "не показано, чего рынок уже ждёт и чем отличается наш сценарий")
    fails, labels = [], []
    out = EV.check_market_view({"market_view": mv}, fails, labels) or {}
    if fails:
        return _pt(False, fails[0].replace("рынок/мы: ", ""))
    kind = out.get("kind")
    if kind in ("flow", "rule"):
        return _pt(False, "расхождение с рынком названо, но цифрами не показано")
    if kind == "magnitude":
        rel = out.get("разрыв_отн")
        if rel is None or abs(rel) < EV.MIN_GAP_REL:
            return _pt(False, "наш сценарий почти совпадает с ожиданиями рынка")
        return _pt(True, f"наше значение отличается от ожиданий рынка на {rel * 100:+.0f}% (источник ожиданий указан)")
    return _pt(True, "рынок и мы ждём события в разные сроки (обе даты названы)")


def _p_why(reason, basis, rules):
    n = int((rules.get("пороги") or {}).get("why_min_chars", 40))
    if len(str(reason or "").strip()) < n:
        return _pt(None if not str(reason or "").strip() else False,
                   "не объяснено, почему рынок это пропускает")
    if not basis:
        return _pt(False, "причина названа, но не привязана к проверенному факту или к тому, кто продаёт и почему")
    return _pt(True, "причина названа и привязана к факту")


def _p_precedent(prec):
    if not isinstance(prec, dict) or not prec:
        return _pt(None, "похожий случай из прошлого не приведён")
    if not _http(prec.get("url")):
        return _pt(False, "похожий случай приведён без ссылки")
    amp = str(prec.get("amplitude") or prec.get("ход") or "")
    if not PI.parse_amplitude(amp):
        return _pt(False, "у похожего случая нет числа — на сколько тогда сдвинулась цена")
    return _pt(True, "похожий случай — со ссылкой и числом хода цены")


def _p_sector(sector):
    if not isinstance(sector, dict) or sector.get("против") is None:
        return _pt(None, (sector or {}).get("словами") or "состояние сектора не измерено")
    return _pt(not sector["против"], sector.get("словами") or "")


def _p_asym(ratio, up, dn, rules):
    thr = float((rules.get("пороги") or {}).get("асимметрия", 2.0))
    if ratio is None:
        return _pt(None, "нет цен входа, цели и выхода — соотношение выигрыша и потери не посчитать")
    txt = f"возможный выигрыш {up:g}% против возможной потери {dn:g}% — {ratio:g} к 1"
    return _pt(ratio >= thr - 1e-9, txt + ("" if ratio >= thr - 1e-9 else f" (нужно не меньше {thr:g} к 1)"))


def _p_court_answered(critique):
    cr = critique if isinstance(critique, dict) else {}
    objs = [o for o in (cr.get("возражения") or []) if isinstance(o, dict)]
    if not str(cr.get("семейство") or "").strip() or not objs:
        return _pt(None, "независимой проверки моделью другого разработчика не было")
    bad = [o for o in objs if o.get("существенное") and len(str(o.get("ответ") or "")) < 20]
    if bad:
        return _pt(False, f"осталось существенных возражений без ответа: {len(bad)}")
    return _pt(True, "на все существенные возражения другой модели есть ответ")


# ── карточка охотника ───────────────────────────────────────────────────────────────────────────────
HARD_RU = {"ФАКТЫ": "факты оформлены не по правилам проверки (ссылка, дата публикации, определение показателя)",
           "ТОРГУЕМОСТЬ": "бумагу нельзя купить как положено (тикер, направление или цена ниже $5)",
           "ВЫХОД": "нет правила выхода (цена входа, цель, стоп и условие «что меня опровергнет»)",
           "СЧЁТ": "прогноз нельзя честно сверить (порог, срок или свежая цена)"}


def points_from_card(card, today=None, rules=None, court=None, sector=None):
    """→ (пункты {id: {...}}, брак [..], факты_о_ставке {...})."""
    rules = rules or load_rules()
    today = today or _today()
    gate = G.check(card, today=today, evidence=True, rules_v3=True)
    # брак — словами для владельца (по номеру правила knowledge/idea_rules.yaml), подробности — в «брак_подробно»
    tags = [t for t, _ in (gate.get("computed") or {}).get("по_правилам") or [] if IR.kind(t) == IR.HARD]
    blockers = [HARD_RU.get(t, "нарушено правило " + str(t)) for t in dict.fromkeys(tags)]
    if gate.get("fails") and not blockers:
        blockers = ["карточка не прошла обязательные проверки"]
    direction = str(card.get("direction") or "").strip().lower()
    spec = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    mv = card.get("market_view") if isinstance(card.get("market_view"), dict) else {}
    pm = G.price_math(direction, spec.get("last_close"), card.get("trigger_close"), card.get("target_price"),
                      card.get("stop_price"), card.get("entry_limit")) if direction in G.DIR_MAP else {}
    P = {}
    P[1] = _p_chain(card.get("chain")) if card.get("chain") else _pt(None, "звенья от события до денег компании не показаны")
    P[2] = _p_materiality(card.get("materiality"))
    P[3] = _p_durability(card.get("durability"))
    P[4] = _p_market(mv)
    who = str(card.get("who_sells") or "").strip()
    P[5] = _p_why(mv.get("why_different") or card.get("why_not_priced"),
                  mv.get("основано_на") or (who if len(who) >= 40 else ""), rules)
    P[6] = _p_precedent(card.get("precedent"))
    P[7] = _p_sector(sector)
    P[8] = _p_asym(round(pm["асимметрия"], 2) if pm.get("асимметрия") else None,
                   pm.get("потенциал_от_входа"), pm.get("риск_от_входа"), rules)
    cat = card.get("catalyst") if isinstance(card.get("catalyst"), dict) else {}
    cd = _date(cat.get("date"))
    tb = TC.check_basis(card.get("target_basis"), card.get("target_price"))
    if not cd or not str(cat.get("what") or "").strip():
        P[9] = _pt(False, "нет события с датой")
    elif cd < today:
        P[9] = _pt(False, f"событие ({cd}) уже прошло")
    elif tb.get("status") != "обоснована":
        P[9] = _pt(False, f"событие есть ({cd}), но цель цены не подкреплена расчётом")
    else:
        P[9] = _pt(True, f"событие {cd}; цель пересчитана по формуле")
    cv = str((court or {}).get("verdict") or "").upper() if isinstance(court, dict) else ""
    if cv == "PASS":
        P[10] = _pt(True, "вторая проверка моделями других разработчиков возражений не оставила")
    elif cv == "FIX":
        P[10] = _pt(False, "вторая проверка оставила сомнение без ответа")
    elif cv == "FATAL":
        P[10] = _pt(False, "вторая проверка показала: бизнес компании не связан с поводом")
        blockers.append("вторая проверка: бизнес компании не связан с поводом")
    else:
        P[10] = _p_court_answered(card.get("критика"))
    facts = {"брак_подробно": [str(x)[:300] for x in (gate.get("fails") or [])[:8]],
             "тикер": str(card.get("ticker") or "").upper(), "имя": card.get("name"), "направление": direction,
             "цена": spec.get("last_close"), "дата_цены": spec.get("last_close_date"),
             "цель": card.get("target_price"), "стоп": card.get("stop_price"), "вход": pm.get("вход")}
    return P, blockers, facts


# ── гипотеза поиска ─────────────────────────────────────────────────────────────────────────────────
def _hyp_data(h):
    """Данные гипотезы; модель иногда вкладывает их в «быстрая_проверка» — берём и оттуда (верхний уровень главнее)."""
    import search_queue as SQ
    return SQ.unwrap(dict(h.get("данные") or {}))


MISPRICED_RE = re.compile(r"упал\w* зря|зря упал|вынужденн|недооцен|переоцен|вырос\w* зря|mispric|forced", re.I)


def is_mispriced_type(d):
    """Идея типа «упал зря»: ставка не на прирост прибыли от события, а на то, что рынок ошибся в цене (бумагу
    продали вместе со всеми, вынужденный продавец). Тип называет автор (поле тип_идеи), обоснованность судит судья."""
    return bool(MISPRICED_RE.search(str(d.get("тип_идеи") or "")))


def fetch_valuation(sym, years=5):
    """Оценка бумаги против собственной истории — код (ops/multiples_view). Сбой или нет данных → None."""
    try:
        import multiples_view as MV
        return MV.view(sym, years)
    except Exception:                                        # noqa: BLE001 — оценка не роняет вердикт
        return None


def _p_undervalued(val, long_, rules):
    """П.2 для идеи «упал зря» (владелец 22.11): размер недооценки вместо прироста прибыли. Прогон 22.11: Arista
    отклонена автором из-за пункта «размер» — у такой идеи прироста прибыли от события нет по определению."""
    thr = float((rules.get("пороги") or {}).get("недооценка", 0.20))
    yrs = int((rules.get("пороги") or {}).get("недооценка_лет", 5))
    if not isinstance(val, dict) or not val.get("ok"):
        return _pt(None, "оценка бумаги против её собственной истории не измерена")
    for key, name in (("pe", "цена к прибыли"), ("ps", "цена к выручке")):
        m = val.get(key) if isinstance(val.get(key), dict) else {}
        now, med = m.get("сейчас"), m.get("медиана")
        if m.get("ok") and _num(now) and _num(med) and now > 0 and med > 0:
            gap = now / med - 1                             # −0.25 = на четверть дешевле своей медианы
            txt = f"{name} {now:g} при медиане за {m.get('лет') or yrs} лет {med:g}"
            if long_:
                if gap <= -thr:
                    return _pt(True, f"бумага стоит на {-gap * 100:.0f}% дешевле своей истории ({txt})")
                return _pt(False, f"бумага не дешевле своей истории хотя бы на {thr * 100:.0f}%: {txt}"
                                  + (f" (дешевле на {-gap * 100:.0f}%)" if gap < 0 else f" (дороже на {gap * 100:.0f}%)"))
            if gap >= thr:
                return _pt(True, f"бумага стоит на {gap * 100:.0f}% дороже своей истории ({txt})")
            return _pt(False, f"бумага не дороже своей истории хотя бы на {thr * 100:.0f}%: {txt}")
    return _pt(None, "оценка бумаги против её собственной истории не измерена (нет прибыли и выручки для расчёта)")


# Пункты, которые кроме кода проверяет независимый судья (сектор и выигрыш/потеря — чистый расчёт, судья не нужен)
JUDGED = {1: "КАНАЛ", 2: "РАЗМЕР", 3: "СРОК", 4: "РЫНОК", 5: "ПОЧЕМУ", 6: "ПРЕЦЕДЕНТ", 9: "СОБЫТИЕ"}


def _apply_judge(h, d, P, blockers, rules):
    """Прогон DHT 22.11: 9 из 10 при том, что «чего ждёт рынок» сравнивал разные кварталы, а ответ на возражение
    критика был неверен по факту, — код проверял форму. Теперь пункт зачтён, только если согласны И код (есть число
    и ссылка), И независимый судья (довод по делу). Пункт 10 — по решению судьи «снято / не снято», а не по
    наличию ответа. Судьи нет (или он смотрел прежние доказательства) → «ставка» невозможна.
    → {'семейство','модель','когда'} | None."""
    import search_queue as SQ
    j, why = SQ.judgement(h)
    if j is None:
        wait_thr = int((rules.get("решения") or {}).get("ожидание", 6))
        if sum(1 for p in P.values() if p["зачтён"] is True) >= wait_thr:
            blockers.append(why + " — без него ставка невозможна")
        return None
    for i, code in JUDGED.items():
        v = (j.get("пункты") or {}).get(code)
        if P[i]["зачтён"] is True and isinstance(v, dict) and v.get("по_делу") is False:
            P[i] = _pt(False, "судья: довод не по делу — " + (v.get("почему") or "причина не названа"))
    cr = d.get("критика") if isinstance(d.get("критика"), dict) else {}
    objs = [(str(o.get("n") or i), o) for i, o in enumerate(cr.get("возражения") or [], 1) if isinstance(o, dict)]
    subst = [(n, o) for n, o in objs if o.get("существенное")]
    if objs:
        left = [(n, (j.get("возражения") or {}).get(n) or {}) for n, _o in subst
                if ((j.get("возражения") or {}).get(n) or {}).get("снято") is not True]
        if left:
            n0, v0 = left[0]
            P[10] = _pt(False, f"судья: не снято существенных возражений — {len(left)} (№{n0}: "
                               f"{v0.get('почему') or 'судья его не оценил'})")
        else:
            P[10] = _pt(True, "судья: все существенные возражения критика сняты ответами")
    return {k: j.get(k) for k in ("семейство", "модель", "когда")}


def _hyp_name(h, long_):
    """Название компании для подписи. У идеи на падение поле «получатель» — тот, кто выигрывает от падения
    (22.11: вердикт вышел с подписью «Короткие продавцы EQIX»), поэтому там имя не берём — остаётся тикер."""
    name = str(h.get("получатель") or "").split(" — ")[0].strip()[:60]
    if not name or not long_ or re.search(r"продавц|шорт|short|держател[ьи] пут", name, re.I):
        return None
    return name


def points_from_hypothesis(h, today=None, rules=None, sector=None, valuation_fn=None):
    rules = rules or load_rules()
    today = today or _today()
    d = _hyp_data(h)
    long_ = str(d.get("направление") or "long") != "short"
    direction = "вверх" if long_ else "вниз"
    blockers = []
    facts = [f for f in (d.get("факты") or []) if isinstance(f, dict) and _http(f.get("url"))]
    if len(facts) < 2:
        blockers.append("меньше двух опорных фактов со ссылкой")
    sc = d.get("сценарии") if isinstance(d.get("сценарии"), dict) else {}
    px = sc.get("цена") if _num(sc.get("цена")) else d.get("цена")
    base = (sc.get("базовый") or {}) if isinstance(sc.get("базовый"), dict) else {}
    adv = (sc.get("неблагоприятный") or {}) if isinstance(sc.get("неблагоприятный"), dict) else {}
    if not (_num(px) and _num(adv.get("цена"))):
        blockers.append("нет правила выхода: не посчитан неблагоприятный сценарий от текущей цены")
    mv = d.get("market_view") if isinstance(d.get("market_view"), dict) else {}
    P = {}
    chain = d.get("chain") or d.get("цепочка")
    P[1] = _p_chain(chain) if chain else _pt(None, "звенья от события до денег компании с источниками не записаны")
    if is_mispriced_type(d):                       # «упал зря»: размер недооценки вместо прироста прибыли
        tk = str(h.get("тикер") or "").upper()
        val = (valuation_fn or fetch_valuation)(tk if "." in tk else tk + ".US") if tk and valuation_fn is not False else None
        P[2] = _p_undervalued(val, long_, rules)
    else:
        P[2] = _p_materiality(d.get("materiality"))
    P[3] = _p_durability(d.get("durability") or d.get("устойчивость"))
    P[4] = _p_market(mv)
    P[5] = _p_why(mv.get("why_different"), mv.get("основано_на"), rules)
    prec = d.get("precedent") or d.get("прецедент")
    P[6] = _p_precedent(prec)
    if P[6]["зачтён"] is None and _num(d.get("вероятность")) and \
            re.search(r"\d+\s*(?:из|/)\s*\d+", str(d.get("основание_вероятности") or "")):
        P[6] = _pt(True, "есть счёт похожих случаев: " + str(d.get("основание_вероятности"))[:120])
    P[7] = _p_sector(sector)
    ratio, up, dn = IS.asymmetry(px, base.get("цена"), adv.get("цена"), direction)
    P[8] = _p_asym(ratio, up, dn, rules)
    n = int((rules.get("пороги") or {}).get("reason_min_chars", 20))
    cat = d.get("катализатор") if isinstance(d.get("катализатор"), dict) else {}
    cd = _date(cat.get("date") or cat.get("дата") or d.get("горизонт"))
    basis = str(base.get("основание") or "")
    if not cd:
        P[9] = _pt(None if not d.get("горизонт") else False, "нет события или срока с датой")
    elif cd < today:
        P[9] = _pt(False, f"срок ({cd}) уже прошёл")
    elif not (_num(base.get("цена")) and len(basis) >= n and re.search(r"\d", basis)):
        P[9] = _pt(False, f"срок есть ({cd}), но цель цены не подкреплена расчётом")
    else:
        P[9] = _pt(True, f"срок {cd}; цель {base['цена']:g} с расчётом")
    P[10] = _p_court_answered(d.get("критика"))
    judge = _apply_judge(h, d, P, blockers, rules)
    info = {"судья": judge, "тип_идеи": "упал зря" if is_mispriced_type(d) else "событие", "тикер": str(h.get("тикер") or "").upper(), "имя": _hyp_name(h, long_),
            "направление": direction, "цена": px, "дата_цены": sc.get("дата_цены") or d.get("дата_цены"),
            "цель": base.get("цена"), "стоп": adv.get("цена"), "вход": px}
    return P, blockers, info


# ── вердикт ─────────────────────────────────────────────────────────────────────────────────────────
def _sector(ticker, direction, today, sector_fn):
    if not ticker or sector_fn is False:
        return None
    try:
        if sector_fn is None:
            import sector_view as SV
            sector_fn = SV.for_ticker
        sym = ticker if "." in ticker else ticker + ".US"
        return sector_fn(sym, direction or "вверх", today)
    except Exception as e:                                   # noqa: BLE001 — сектор не роняет вердикт
        return {"против": None, "словами": f"состояние сектора не измерено ({type(e).__name__})"}


def _verdict(P, blockers, info, rules, source, today):
    sc = IS.score(P, rules, blockers)
    return {"когда": today.isoformat(), "источник": source, **info, **sc, "версия_листа": rules.get("версия")}


def verdict_for_card(card, today=None, rules=None, court=None, sector_fn=None, source="карточка охотника"):
    rules = rules or load_rules()
    today = today or _today()
    sector = _sector(str(card.get("ticker") or "").upper(), str(card.get("direction") or "").lower(), today, sector_fn)
    P, blockers, info = points_from_card(card, today, rules, court, sector)
    return _verdict(P, blockers, info, rules, source, today)


REJECT_RU = {"priced_in": "эффект уже в цене", "mechanism_refuted": "механизм опровергнут",
             "instrument_unfit": "инструмент не подходит", "no_data": "не хватает данных"}


def verdict_for_hypothesis(h, today=None, rules=None, sector_fn=None, source="поиск", valuation_fn=None):
    """Гипотеза поиска → вердикт. Отклонённая по существу — сразу «архив» с причиной (балл не считается)."""
    rules = rules or load_rules()
    today = today or _today()
    if h.get("вердикт") == "rejected":
        rej = h.get("отказ") or {}
        return {"когда": today.isoformat(), "источник": source, "тикер": str(h.get("тикер") or "").upper(),
                "имя": str(h.get("получатель") or "").split(" — ")[0][:60] or None, "балл": None,
                "из": len(rules.get("пункты") or []), "решение": IS.ARCHIVE, "решение_словами": IS.DECISION_RU[IS.ARCHIVE],
                "отклонена": True, "причина": (REJECT_RU.get(rej.get("вид"), "отклонена") + ": "
                                               + str(rej.get("причина") or "")[:220]).strip(": "),
                "пункты": [], "не_хватает": [], "брак": [], "версия_листа": rules.get("версия")}
    d = _hyp_data(h)
    direction = "вверх" if str(d.get("направление") or "long") != "short" else "вниз"
    sector = _sector(str(h.get("тикер") or "").upper(), direction, today, sector_fn)
    P, blockers, info = points_from_hypothesis(h, today, rules, sector, valuation_fn)
    v = _verdict(P, blockers, info, rules, source, today)
    if h.get("нужно_данных"):
        v["ждёт_данных"] = [str(x)[:200] for x in h["нужно_данных"][:4]]
    return v


def search_verdicts(s, today=None, rules=None, sector_fn=None):
    """Все гипотезы поиска → вердикты, по убыванию балла (отклонённые по существу — в конце)."""
    rules = rules or load_rules()
    src = f"поиск «{str(s.get('тема') or '')[:60]}»"
    out = []
    for h in s.get("гипотезы") or []:
        v = verdict_for_hypothesis(h, today, rules, sector_fn, source=src)
        v["гипотеза"], v["поиск"] = h.get("id"), s.get("id")
        out.append(v)
    return sorted(out, key=lambda v: (v["балл"] is None, -(v["балл"] or 0)))


# ── текст для владельца (пишет КОД) ─────────────────────────────────────────────────────────────────
def _label(v):
    t = str(v.get("тикер") or "").split(".")[0]
    return f"{v['имя']} ({t})" if v.get("имя") else t


def one_line(v):
    if v.get("отклонена"):
        return f"{_label(v)} — архив: {v['причина']}"
    return f"{_label(v)} — {v['балл']} из {v['из']}, {v['решение_словами']}"


def render(v, track_line=None):
    """Вердикт словами. Ни одного числа «от модели»: всё из посчитанного кодом."""
    if v.get("отклонена"):
        return f"Вердикт по {_label(v)}: архив. Причина — {v['причина']}."
    L = [f"Вердикт по {_label(v)}: {v['балл']} из {v['из']} — {v['решение_словами']}."]
    ok = [p for p in v["пункты"] if p["зачтён"] is True]
    bad = [p for p in v["пункты"] if p["зачтён"] is not True]
    if ok:
        L.append("Зачтено:")
        mis = v.get("тип_идеи") == "упал зря"
        L += [("• " + ("бумага заметно дешевле своей истории" if mis and v.get("направление") != "вниз" else
                       "бумага заметно дороже своей истории") if mis and p["код"] == "РАЗМЕР" else f"• {p['словами']}")
              + (f" — {p['почему']}" if p.get("почему") and (p["код"] in ("СЕКТОР", "ВЫИГРЫШ", "РЫНОК")
                                                              or mis and p["код"] == "РАЗМЕР") else "")
              for p in ok]
    if bad:
        L.append("Не зачтено:")
        L += [f"• {p['почему'] or p['словами']}" for p in bad]
    if v.get("направление") == "вниз" and v["решение"] != IS.ARCHIVE:
        L.append("Это идея на ПАДЕНИЕ бумаги. Способы: продажа взаймы (убыток не ограничен, нужен маржинальный счёт), "
                 "обратный фонд, опцион «пут» (потеря не больше премии) или просто не покупать — что доступно, зависит "
                 "от твоего брокера.")
    if v.get("судья"):
        L.append(f"Доводы по существу проверил независимый судья — модель третьего разработчика ({v['судья'].get('семейство')}).")
    if v.get("брак"):
        L.append("Мешает ставке при любом балле: " + "; ".join(str(b)[:160] for b in v["брак"][:3]) + ".")
    thr = v["пороги"]["ставка"]
    if v["решение"] == IS.BET:
        L.append("Что дальше: лист ставки — сколько можно выиграть и потерять в долларах; решение о ставке — твоё.")
    elif v["решение"] == IS.WAIT:
        need = v["до_ставки"]
        L.append((f"Чтобы стать кандидатом на ставку, нужно закрыть ещё {need} из незачтённых пунктов"
                  if need else "Балла хватает, но сначала нужно снять то, что мешает ставке") +
                 (" — и снять то, что мешает ставке." if need and v.get("брак") else "."))
    else:
        L.append(f"Почему архив: до листа ожидания нужно {v['пороги']['ожидание']} из {v['из']}, до ставки — {thr}.")
    if v.get("ждёт_данных"):
        L.append("Ждёт данных: " + "; ".join(v["ждёт_данных"][:2]))
    L.append(DISCLAIMER + (" " + track_line if track_line else ""))
    return "\n".join(L)


def _short(text, n=70):
    """Название поиска не длиннее n знаков: режем по границе слова, а не посреди («…(PA» → «…кибербезопасность…»)."""
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(" ,;:—–-(")
    return (cut or text[:n]) + "…"


def render_search(vs, topic=None, limit=3):
    """Итог поиска одним блоком: лучшие кандидаты по баллу + отклонённые одной строкой каждая."""
    live = [v for v in vs if not v.get("отклонена")]
    dead = [v for v in vs if v.get("отклонена")]
    L = ["Итог комитета" + (f" по поиску «{_short(topic)}»" if topic else "") + ":"]
    if not live:
        L.append("ни одна гипотеза не дошла до оценки — все отклонены по существу.")
    for v in live[:limit]:
        L.append("• " + one_line(v) + (". Не хватает: " + "; ".join(p["почему"] or p["словами"] for p in v["не_хватает"][:3])
                                         + ("…" if len(v["не_хватает"]) > 3 else "") if v["не_хватает"] else ""))
    if dead:
        L.append("В архиве: " + "; ".join(f"{_label(v)} ({v['причина'].split(':')[0]})" for v in dead[:8]) + ".")
    L.append(DISCLAIMER)
    return "\n".join(L)


# ── запись и счёт метода ────────────────────────────────────────────────────────────────────────────
def save(v, out_dir=None):
    """Вердикт — датированная запись ДО исхода (по ней потом считается счёт по группам балла)."""
    d = pathlib.Path(out_dir or OUT)
    d.mkdir(parents=True, exist_ok=True)
    rec = {k: v.get(k) for k in ("когда", "источник", "тикер", "имя", "направление", "цена", "дата_цены", "цель",
                                 "стоп", "балл", "из", "решение", "брак", "поиск", "гипотеза", "причина",
                                 "версия_листа")}
    rec["зачтены"] = [p["код"] for p in v.get("пункты") or [] if p["зачтён"] is True]
    rec["записано"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with open(d / INDEX, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def saved(out_dir=None):
    p = pathlib.Path(out_dir or OUT) / INDEX
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


def latest_by_ticker(recs):
    """Последний вердикт на бумагу и направление."""
    out = {}
    for r in recs:
        out[(r.get("тикер"), r.get("направление"))] = r
    return list(out.values())


def track(recs=None, prices=None, rules=None, out_dir=None):
    """Счёт метода по группам балла: ПЕРВЫЙ вердикт по бумаге → условный вход по цене вердикта → результат
    против эталона 60/40 (те же функции, что в общем счёте ops/ledger.py). → {группа: {n, средняя, сверх_эталона}}"""
    import ledger as LG
    rules = rules or load_rules()
    recs = recs if recs is not None else saved(out_dir)
    prices = prices or LG.Prices()
    first = {}
    for r in recs:
        if r.get("балл") is None or not _num(r.get("цена")) or not _date(r.get("дата_цены") or r.get("когда")):
            continue
        first.setdefault((r["тикер"], r.get("направление")), r)
    groups = {IS.BET: [], IS.WAIT: [], IS.ARCHIVE: []}
    for (t, direction), r in first.items():
        sym = t if "." in t else t + ".US"
        d0 = str(r.get("дата_цены") or r["когда"])[:10]
        dirn = "below" if direction == "вниз" else "above"
        ret = LG._ret(prices.series(sym, d0), r["цена"], r.get("стоп"), dirn)
        if ret is None:
            continue
        b, _ = LG.bench_return(prices, d0, ret[3])
        res = (1 if dirn == "above" else -1) * ret[4]
        groups[IS.band(r["балл"], rules)].append((res, None if b is None else res - b))
    out = {}
    for g, rows in groups.items():
        ex = [x[1] for x in rows if x[1] is not None]
        out[g] = {"n": len(rows), "средняя": round(sum(x[0] for x in rows) / len(rows), 4) if rows else None,
                  "сверх_эталона": round(sum(ex) / len(ex), 4) if ex else None}
    return out


def track_line(tr, rules=None):
    rules = rules or load_rules()
    dec = rules.get("решения") or {}
    names = {IS.BET: f"{dec.get('ставка', 8)}+", IS.WAIT: f"{dec.get('ожидание', 6)}–{int(dec.get('ставка', 8)) - 1}",
             IS.ARCHIVE: f"ниже {dec.get('ожидание', 6)}"}
    parts = []
    for g in (IS.BET, IS.WAIT, IS.ARCHIVE):
        t = (tr or {}).get(g) or {}
        if not t.get("n"):
            parts.append(f"с баллом {names[g]} — пока нет")
        else:
            ex = t.get("сверх_эталона")
            parts.append(f"с баллом {names[g]} — {t['n']} шт., в среднем {t['средняя'] * 100:+.1f}%"
                         + (f" ({ex * 100:+.1f} п.п. к эталону)" if ex is not None else ""))
    n = sum(((tr or {}).get(g) or {}).get("n") or 0 for g in (IS.BET, IS.WAIT, IS.ARCHIVE))
    tail = f" Рано судить: оценённых идей {n}, нужно не меньше {MIN_N_TRACK}." if n < MIN_N_TRACK else ""
    return "Счёт метода: идеи " + "; ".join(parts) + "." + tail


def board(out_dir=None, search_dir=None, today=None, rules=None, sector_fn=None, limit=12):
    """Сводка «что заслуживает внимания»: последние сохранённые вердикты + живые гипотезы незавершённых поисков."""
    import search_queue as SQ
    rules = rules or load_rules()
    rows = {(r.get("тикер"), r.get("направление")): r for r in latest_by_ticker(saved(out_dir)) if r.get("балл") is not None}
    for s in _all_searches(search_dir):
        if s.get("статус") == SQ.CANCELLED:
            continue
        for v in search_verdicts(s, today, rules, sector_fn):
            if v.get("балл") is not None:
                rows[(v["тикер"], v.get("направление"))] = v
    out = sorted(rows.values(), key=lambda v: (-(v["балл"] or 0), str(v.get("когда"))))
    return out[:limit]


def render_board(rows, track_text=None):
    if not rows:
        return "Сводка комитета: оценённых идей пока нет."
    L = ["Сводка комитета — что заслуживает внимания (по баллу, сверху лучшие):"]
    cur = None
    for v in rows:
        if v["решение"] != cur:
            cur = v["решение"]
            L.append("")
            L.append({IS.BET: "Кандидаты на маленькую ставку:", IS.WAIT: "Лист ожидания:", IS.ARCHIVE: "Архив:"}[cur])
        L.append(f"• {_label(v)} — {v['балл']} из {v['из']} ({v.get('источник')}, {v.get('когда')})")
    L.append("")
    L.append(DISCLAIMER + (" " + track_text if track_text else ""))
    return "\n".join(L)


DAILY_SEARCHES = ROOT / "reports" / "ideas" / "searches_daily"


def _all_searches(search_dir=None):
    """Поиски владельца в Telegram + самостоятельные ежедневные (у каждого своя папка)."""
    import search_queue as SQ
    out = list(SQ.all_searches(search_dir))
    if search_dir is None and DAILY_SEARCHES.exists():
        for d in sorted(p for p in DAILY_SEARCHES.iterdir() if p.is_dir()):
            out += SQ.all_searches(d)
    return out


# ── не слишком ли строг лист ────────────────────────────────────────────────────────────────────────
CARDS = ROOT / "reports" / "ideas"


def all_evaluated(cards_dir=None, search_dir=None, rules=None, sector_fn=None):
    """Все идеи, по которым считался балл: карточки охотника (на дату охоты) и гипотезы всех поисков, не
    отклонённые по существу (отменённые поиски тоже — вопрос о листе, а не о судьбе идеи)."""
    import search_queue as SQ
    rules = rules or load_rules()
    out = []
    for f in sorted(pathlib.Path(cards_dir or CARDS).glob("idea_*.card.json")):
        try:
            card = json.loads(f.read_text(encoding="utf-8"))
            out.append(verdict_for_card(card, _date(f.name.split("idea_")[1][:10]) or _today(), rules,
                                        sector_fn=sector_fn))
        except Exception:                                   # noqa: BLE001 — битая карточка не роняет отчёт
            continue
    for s in _all_searches(search_dir):
        out += [v for v in search_verdicts(s, None, rules, sector_fn) if v.get("балл") is not None]
    return out


def render_strictness(st):
    """Отчёт «не слишком ли строг лист» словами (пишет код)."""
    n, thr = st["оценено"], st["пороги"]
    L = [f"Не слишком ли строг лист комитета: оценено идей — {n}, лучший балл — "
         f"{st['лучший_балл'] if st['лучший_балл'] is not None else 'нет'} из {len(st['пункты'])}; до листа ожидания "
         f"({thr['ожидание']}+) дошло {st['дошли_до_ожидания']}, до ставки ({thr['ставка']}+) — {st['дошли_до_ставки']}.",
         "", "По пунктам (зачтён / проверен и не прошёл / данных никто не собрал):"]
    for r in st["пункты"]:
        L.append(f"• {r['id']}. {r['словами']}: {r['зачтён']} / {r['не_прошло']} / {r['не_измерено']}")
    L.append("")
    L.append(f"Пунктов, которые хоть раз кто-то прошёл: {st['потолок_на_практике']} из {len(st['пункты'])} — выше этого "
             f"балла пока не мог набрать никто.")
    L.append(f"Проверено глубоко (измерено не меньше {st['глубокая_от']} пунктов из {len(st['пункты'])}): "
             f"{st['глубоких']} идей, из них до листа ожидания дошло {st['глубоких_дошло']}.")
    if st["не_собирается"]:
        L.append("Данные почти никогда не собираются: " + "; ".join(r["словами"] for r in st["не_собирается"]) + ".")
    if st["не_проходит"]:
        L.append("Проверяется, но не проходит ни у кого: " + "; ".join(r["словами"] for r in st["не_проходит"]) + ".")
    L.append("")
    L.append({
        "рано": f"Вывод: рано судить — оценено меньше {thr['мин_n']} идей и глубоко проверено меньше "
                f"{st['глубоких_для_вывода']}.",
        "норма": "Вывод: лист проходим — идеи доходят до листа ожидания.",
        "мелко": f"Вывод: до листа ожидания не дошла ни одна идея, но судить о строгости листа нельзя: глубоко "
                 f"проверено {st['глубоких']} из нужных {st['глубоких_для_вывода']} идей — доказательства по "
                 f"большинству пунктов никто не собирал. Лечится глубиной поиска, а не снижением планки.",
        "строго": f"Вывод: ЛИСТ СЛИШКОМ СТРОГ — глубоко проверено {st['глубоких']} идей, и ни одна не дошла до листа "
                  f"ожидания. Нужно слово владельца: какие пункты смягчить или убрать.",
    }[st["сигнал"]])
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Инвестиционный комитет: балл X из 10 и решение по идее")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("card")
    p.add_argument("path")
    p.add_argument("--today", default=None)
    p.add_argument("--court", default=None, choices=("PASS", "FIX", "FATAL"))
    p.add_argument("--save", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("search")
    p.add_argument("id")
    p.add_argument("--today", default=None)
    p.add_argument("--save", action="store_true")
    p.add_argument("--json", action="store_true")
    sub.add_parser("board")
    sub.add_parser("track")
    sub.add_parser("strictness")
    a = ap.parse_args(argv)
    rules = load_rules()
    if a.cmd == "card":
        card = json.loads(pathlib.Path(a.path).read_text(encoding="utf-8"))
        v = verdict_for_card(card, _date(a.today) if a.today else None, rules,
                             {"verdict": a.court} if a.court else None)
        if a.save:
            save(v)
        print(json.dumps(v, ensure_ascii=False, indent=1) if a.json else render(v))
    elif a.cmd == "search":
        import search_queue as SQ
        ss = SQ.all_searches()
        s = (sorted(ss, key=lambda x: x.get("создан") or "")[-1] if a.id == "latest" else SQ.load(a.id))
        vs = search_verdicts(s, _date(a.today) if a.today else None, rules)
        if a.save:
            for v in vs:
                if v.get("балл") is not None:
                    save(v)
        if a.json:
            print(json.dumps(vs, ensure_ascii=False, indent=1))
        else:
            print(render_search(vs, s.get("тема")))
            for v in [x for x in vs if not x.get("отклонена")][:2]:
                print("\n" + render(v))
    elif a.cmd == "board":
        print(render_board(board(rules=rules), track_line(track(rules=rules), rules)))
    elif a.cmd == "strictness":
        print(render_strictness(IS.strictness(all_evaluated(rules=rules), rules)))
    else:
        print(track_line(track(rules=rules), rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
