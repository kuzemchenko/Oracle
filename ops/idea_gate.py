# -*- coding: utf-8 -*-
"""ops/idea_gate.py — ДЕТЕРМИНИРОВАННЫЙ ШЛАГБАУМ идеи охотника (архитектура 02.11, вердикт
владельца: «ты даёшь идею и тут же её убиваешь — значит, не там искал; поправь архитектуру»).

Прежняя охота: разведчики искали поводы в новостях → суду велели «при сомнении — опровергай»
→ 0 выживших по конструкции → владельцу шло «наблюдение» с приговором. Новая: планка идеи
задаётся ДО поиска (knowledge/idea_bar.yaml), разведчик сам прогоняет кандидата через этот
шлагбаум и приносит только прошедших; суд дальше проверяет и чинит, а убивать вправе лишь
за жёсткие провалы, которые здесь же и считаются кодом.

Жёсткие критерии (Инв#6 — проверка кодом, не мнением):
  1. карточка полная (тикер, направление, катализатор, факты, прецедент, стоп-условия, §9);
  2. катализатор датирован и лежит в окне [сегодня … +13 недель];
  3. горизонт сверки 3–13 недель (зона Тетлока);
  4. ≥3 факта с URL и датой, ≥2 разных источника (П8);
  5. прецедент с ЧИСЛОВОЙ амплитудой, и ход с даты повода НЕ съел её (mathlib.priced_in:
     вердикт «большая часть в цене»/«перелёт» — провал); ход считает КОД из закрытия в день
     повода и последней цены (перепроверка №2 03.11: раньше — число со слов LLM);
  6. асимметрия: потенциал/риск ≥ 1.5 от ХУДШЕГО входа — из цен цели, стопа и входа, кодом;
  7. ≥1 стоп-условие с датой проверки не позже сверки;
  8. §9-спека разрешима, порог заякорен к свежей цене (0.5×..2×, не на уже-истинной
     стороне), направление согласовано с идеей; сырая вероятность в [0.05..0.95] —
     только для печати и счёта, владельцу НЕ показывается.

CLI: python3 ops/idea_gate.py card.json [--today YYYY-MM-DD] → JSON-вердикт; код выхода 0/1.
"""
import datetime
import json
import pathlib
import re
import sys
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import priced_in as PI          # noqa: E402
from mathlib import sealing as SEAL          # noqa: E402

try:
    from ops import evidence_check as EV     # noqa: E402
except ImportError:                          # запуск из ops/ (бот)
    import evidence_check as EV              # noqa: E402

# Аудит 07.11: проверка СОДЕРЖАНИЯ доводов (ops/evidence_check.py) обязательна для охот с этой даты.
# Идеи раньше неё уже запечатаны и ведутся своими проверками — их плановые обновления не должны
# внезапно браковаться новыми полями; по ним доказательства показываются предупреждением.
EVIDENCE_FROM = datetime.date(2026, 11, 8)

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}\.US$")
MIN_DAYS, MAX_DAYS = 21, 91                  # 3–13 недель
MIN_FACTS, MIN_DOMAINS = 3, 2
MIN_ASYM = 1.5
P_MIN, P_MAX = 0.05, 0.95
MAX_PRICE_AGE_DAYS = 10
MIN_PRICE = 5.0                              # Ж1 разбора 02.11: копеечные бумаги — не идея дня
TOL_PP = 2.0                                 # допуск: заявленный % против пересчёта по ценам, п.п.
DIR_MAP = {"вверх": "above", "вниз": "below"}
# Перепроверка №2 (03.11): ход с повода, потенциал и риск больше НЕ берутся со слов LLM — карточка
# несёт ЦЕНЫ (закрытие в день повода, цель, стоп, худший вход), а проценты считает этот код; на
# сервере цены заменяются серверными котировками (hunter_seal.server_check).
REQUIRED = ("ticker", "name", "direction", "catalyst", "thesis", "facts", "precedent",
            "move_since_trigger_pct", "upside_pct", "downside_pct", "who_sells",
            "kill_conditions", "spec9", "trigger_date", "trigger_close", "target_price", "stop_price")


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def price_math(direction, last_close, trigger_close, target, stop, entry_limit=None):
    """Проценты идеи из ЦЕН (Инв#6). → {'ход_с_повода', 'потенциал', 'риск' (от цены закрытия),
    'вход', 'потенциал_от_входа', 'риск_от_входа', 'асимметрия', 'fails'}.
    Худший вход: для «вверх» — «не дороже entry_limit», для «вниз» — «не дешевле»; без него — цена."""
    out = {"fails": []}
    px, tc, tp, sp = (_num(x) for x in (last_close, trigger_close, target, stop))
    el = _num(entry_limit)
    if None in (px, tc, tp, sp) or min(px, tc, tp, sp) <= 0:
        out["fails"].append("нет цен для расчёта (закрытие, закрытие в день повода, цель, стоп)")
        return out
    up = direction == "вверх"
    eb = el if el and el > 0 else px
    out["вход"] = eb
    out["ход_с_повода"] = round((px / tc - 1) * 100, 2)

    def gain(x):
        return (tp / x - 1) * 100 if up else (1 - tp / x) * 100

    def loss(x):
        return (1 - sp / x) * 100 if up else (sp / x - 1) * 100
    if (up and px <= sp) or (not up and px >= sp):
        out["fails"].append(f"цена {px:g} уже за стопом {sp:g} — идея сломана до входа")
    if (up and px >= tp) or (not up and px <= tp):
        out["fails"].append(f"цель {tp:g} уже достигнута ценой {px:g}")
    if (up and not sp < eb < tp) or (not up and not tp < eb < sp):
        out["fails"].append(f"цель {tp:g} и стоп {sp:g} не по разные стороны от входа {eb:g}")
        return out
    out["потенциал"], out["риск"] = round(gain(px), 2), round(loss(px), 2)
    g, l = gain(eb), loss(eb)
    out["потенциал_от_входа"], out["риск_от_входа"] = round(g, 2), round(l, 2)
    out["асимметрия"] = round(g / l, 4) if l > 0 else None
    return out


def check(card, today=None, horizon_from=None, evidence=None):
    """Карточка → {'pass', 'fails', 'warnings', 'computed'}. Пустой fails = прошла шлагбаум.
    horizon_from — дата, от которой мерить горизонт 3–13 недель (на сервере — дата охоты: сервер
    печатает с лагом ≤3 дней, и честная идея на нижней границе не должна умирать от задержки
    доставки — перепроверка №2); по умолчанию — today.
    evidence: True — проверка доказательств обязательна (брак), False — только предупреждения,
    None — по дате охоты (horizon_from или today) против EVIDENCE_FROM."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    fails, warns, comp = [], [], {}
    if not isinstance(card, dict):
        return {"pass": False, "fails": ["карточка не объект"], "warnings": [], "computed": {}}
    for f in REQUIRED:
        v = card.get(f)
        if v is None or (isinstance(v, (str, list, dict)) and not v):
            fails.append(f"нет поля «{f}»")
    if fails:
        return {"pass": False, "fails": fails, "warnings": warns, "computed": comp}

    ticker = str(card["ticker"]).strip().upper()
    if not TICKER_RE.match(ticker):
        fails.append(f"тикер не в формате EODHD *.US: {ticker!r}")
    direction = str(card["direction"]).strip().lower()
    if direction not in DIR_MAP:
        fails.append(f"направление не «вверх»/«вниз»: {direction!r}")

    # 2. катализатор в окне
    cat = card["catalyst"] if isinstance(card["catalyst"], dict) else {}
    cd = _date(cat.get("date"))
    if cd is None:
        fails.append("катализатор без даты (YYYY-MM-DD)")
    else:
        delta = (cd - today).days
        comp["катализатор_через_дней"] = delta
        if delta < 0 or delta > MAX_DAYS:
            fails.append(f"катализатор {cd} вне окна [сегодня … +13 нед] ({delta} дн)")
    if not str(cat.get("what") or "").strip():
        fails.append("катализатор без описания")

    # 4. факты с источниками
    facts = card["facts"] if isinstance(card["facts"], list) else []
    # перепроверка №3: «из будущего» — относительно ДНЯ ОХОТЫ (+1 день: охота может перейти через
    # полночь UTC), а не серверного сегодня — иначе через 3 дня проходили бы факты «из завтра»
    facts_until = (horizon_from or today) + datetime.timedelta(days=1)
    good, domains, future = 0, set(), []
    for f in facts:
        if not isinstance(f, dict):
            continue
        url = str(f.get("url") or "")
        fd = _date(f.get("date"))
        if fd is not None and fd > facts_until:
            future.append(str(fd))                 # дата публикации не бывает в будущем (П8)
            continue
        if str(f.get("date_kind") or "").strip().lower() == "проверка":
            continue                               # перепроверка №6: дата ПРОВЕРКИ — не опора (П8), но допустима
        if url.startswith("http") and fd and str(f.get("text") or "").strip():
            good += 1
            domains.add(urlparse(url).netloc.lower().removeprefix("www."))
    comp["фактов_с_источником"], comp["источников"] = good, len(domains)
    if future:
        fails.append(f"факты датированы позже дня охоты ({', '.join(sorted(set(future))[:3])}) — "
                     "дата факта = дата публикации, не проверки (П8)")
    if good < MIN_FACTS:
        fails.append(f"фактов с URL и датой {good} < {MIN_FACTS} (П8)")
    if len(domains) < MIN_DOMAINS:
        fails.append(f"разных источников {len(domains)} < {MIN_DOMAINS} (П8)")

    spec = card["spec9"] if isinstance(card["spec9"], dict) else {}
    px, thr = _num(spec.get("last_close")), _num(spec.get("threshold"))
    pxd = _date(spec.get("last_close_date"))

    # 5–6. ЦЕНЫ → проценты кодом; заявленные проценты обязаны с ними сходиться
    td = _date(card.get("trigger_date"))
    if td is None:
        fails.append("нет даты повода (trigger_date) — ход «с повода» не от чего мерить")
    elif pxd is not None and td > pxd:
        fails.append(f"дата повода {td} позже цены закрытия {pxd}")
    pm = price_math(direction, px, card.get("trigger_close"), card.get("target_price"),
                    card.get("stop_price"), card.get("entry_limit")) if direction in DIR_MAP else {"fails": []}
    fails += pm["fails"]
    comp["цены"] = {k: v for k, v in pm.items() if k != "fails"}
    move = pm.get("ход_с_повода")
    up_px, up_eb, dn_eb = pm.get("потенциал"), pm.get("потенциал_от_входа"), pm.get("риск_от_входа")
    for key, calc in (("move_since_trigger_pct", move), ("upside_pct", up_px), ("downside_pct", pm.get("риск"))):
        decl = _num(card.get(key))
        if calc is not None and (decl is None or abs(decl - calc) > TOL_PP):
            fails.append(f"«{key}» = {card.get(key)} не сходится с ценами карточки ({calc:g}%) — "
                         "проценты считает код, не автор")

    # 5. прецедент и «что уже в цене»
    prec = card["precedent"] if isinstance(card["precedent"], dict) else {}
    amp = str(prec.get("amplitude") or "")
    if not str(prec.get("url") or "").startswith("http"):
        fails.append("прецедент без URL (П8)")
    ranges = PI.parse_amplitude(amp)
    # потолок потенциала — дальний край ПЕРВОГО диапазона прецедента (тот же, что для «в цене»),
    # а не максимум любого процента в строке (перепроверка 02.11)
    far = max(abs(ranges[0]["низ"]), abs(ranges[0]["верх"])) if ranges else 0.0
    comp["амплитуда_дальний_край_пп"] = far
    if move is None:
        fails.append("ход с даты повода не посчитан — без него «не отыграно» не доказать")
    elif not ranges:
        fails.append("амплитуда прецедента не распознана числом")
    elif direction in DIR_MAP:
        # Ревью 02.11: доля «в цене» считается ПО НАПРАВЛЕНИЮ ИДЕИ — ход бумаги в сторону идеи против
        # модуля прецедентной амплитуды.
        r0 = ranges[0]
        lo, hi = sorted((abs(r0["низ"]), abs(r0["верх"])))
        in_dir = move if direction == "вверх" else -move
        pf = PI.played_fraction(in_dir, f"+{lo:g}–{hi:g}%" if lo != hi else f"+{hi:g}%")
        comp["что_в_цене"] = pf
        if "ОТКАЗ" in pf:
            fails.append("доля отыгранности не посчитана: " + pf["ОТКАЗ"])
        elif pf.get("вердикт") in (PI.В_ЦЕНЕ, PI.ПЕРЕЛЁТ):
            fails.append(f"звено уже отыграно числом: {pf['вердикт']} ({pf.get('строка')})")

    # 6. асимметрия — от ХУДШЕГО входа (то, что владелец реально получит), по ценам
    asym = pm.get("асимметрия")
    if up_eb is not None and dn_eb is not None:
        comp["асимметрия"] = round(asym, 2) if asym is not None else None
        if asym is None or asym < MIN_ASYM - 1e-6:
            fails.append(f"асимметрия {asym if asym is not None else '—'} < {MIN_ASYM} "
                         f"(потенциал {up_eb:g}% / риск {dn_eb:g}% от входа {pm.get('вход'):g})")
        if far and up_eb > far * 1.05:
            fails.append(f"потенциал {up_eb:g}% больше прецедентного хода {far:g}% — питч без основы")

    # 8. §9-спека
    rb = _date(spec.get("resolve_by"))
    probs = SEAL.validate_resolvable({**spec, "price_source": spec.get("price_source")
                                      or f"EODHD close {ticker}"})
    fails += [f"§9: {p}" for p in probs]
    if str(spec.get("asset") or "").upper() != ticker:
        fails.append(f"§9: актив спеки {spec.get('asset')!r} ≠ тикер идеи {ticker!r}")
    if direction in DIR_MAP and str(spec.get("direction") or "").lower() != DIR_MAP[direction]:
        fails.append(f"§9: направление спеки {spec.get('direction')!r} не согласовано с «{direction}»")
    if rb is None:
        fails.append("§9: resolve_by без даты")
    else:
        hfrom = horizon_from or today
        hd = (rb - hfrom).days
        comp["горизонт_дней"] = hd
        if not MIN_DAYS <= hd <= MAX_DAYS:
            fails.append(f"§9: горизонт {hd} дн от {hfrom} вне 3–13 недель")
        if rb <= today:
            fails.append("§9: дата сверки уже наступила")
        if cd is not None and cd > rb:
            fails.append("§9: сверка раньше катализатора — вопрос разрешится до события")
    p = _num(spec.get("probability"))
    if p is None or not P_MIN <= p <= P_MAX:
        fails.append(f"§9: сырая вероятность вне [{P_MIN}..{P_MAX}] (для счёта, не для показа)")
    if px is None or px <= 0 or pxd is None:
        fails.append("§9: нет свежей цены закрытия с датой — порог не заякорить (§10)")
    else:
        if px < MIN_PRICE:
            fails.append(f"цена {px} < ${MIN_PRICE:g}: копеечная бумага не годится в идею дня")
        age = (today - pxd).days
        comp["возраст_цены_дней"] = age
        if age > MAX_PRICE_AGE_DAYS:
            fails.append(f"§9: цена закрытия устарела ({age} дн > {MAX_PRICE_AGE_DAYS})")
        if thr is not None:
            if not 0.5 * px <= thr <= 2.0 * px:
                fails.append(f"§9: порог {thr} не заякорен к цене {px} (вне 0.5×..2×)")
            sd = str(spec.get("direction") or "").lower()
            if (sd == "above" and thr <= px) or (sd == "below" and thr >= px):
                fails.append(f"§9: порог {thr} на уже-истинной стороне цены {px} — тривиальный вопрос")
            elif up_px is not None and up_px > 0:
                # цель прогноза — в пределах потенциала (иначе питч +40%, а печать — другое)
                thr_move = (thr / px - 1) * 100 if sd == "above" else (1 - thr / px) * 100
                comp["ход_до_порога_пп"] = round(thr_move, 2)
                if thr_move > up_px * 1.05:
                    fails.append(f"§9: порог требует хода {thr_move:.1f}% — больше потенциала до цели {up_px:g}%")
                elif thr_move < max(0.25 * up_px, 2.0):
                    fails.append(f"§9: порог всего в {thr_move:.1f}% от цены — вопрос «закроется ли выше "
                                 f"сегодняшнего» не проверяет обещанный ход {up_px:g}%")

    # 7. стоп-условия
    kills = card["kill_conditions"] if isinstance(card["kill_conditions"], list) else []
    dated = [k for k in kills if isinstance(k, dict) and str(k.get("what") or "").strip()
             and _date(k.get("check_by"))]
    if not dated:
        fails.append("нет стоп-условия с датой проверки («что меня опровергнет и когда проверю»)")
    elif rb is not None and all(_date(k["check_by"]) > rb for k in dated):
        fails.append("все стоп-условия проверяются позже сверки")

    if not str(card["who_sells"]).strip():
        fails.append("не сказано, кто продаёт нам и почему")

    # 9. доказательства (аудит 07.11): цитаты регуляторики и исключения, проверка «отсутствия»,
    # базис показателей, время события, причинность хода, расчёт цели, вид вероятности
    ev = EV.check(card)
    enforce = evidence if evidence is not None else (horizon_from or today) >= EVIDENCE_FROM
    comp["доказательства"] = {"обязательны": bool(enforce), "брак": ev["fails"], "оговорки": ev["labels"],
                              "цель": (ev.get("target") or {}).get("status")}
    if enforce:
        fails += [f"доказательства: {x}" for x in ev["fails"]]
    else:
        warns += [f"доказательства (для охот с {EVIDENCE_FROM}): {x}" for x in ev["fails"]]
    return {"pass": not fails, "fails": fails, "warnings": warns, "computed": comp}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Шлагбаум идеи охотника (жёсткие критерии кодом)")
    ap.add_argument("card")
    ap.add_argument("--today", default=None)
    ap.add_argument("--strict-evidence", action="store_true",
                    help="проверка доказательств обязательна и для охот раньше EVIDENCE_FROM (перепрогон старых карточек)")
    a = ap.parse_args(argv)
    card = json.loads(pathlib.Path(a.card).read_text(encoding="utf-8"))
    res = check(card, today=_date(a.today) if a.today else None,
                evidence=True if a.strict_evidence else None)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
