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
     вердикт «большая часть в цене»/«перелёт» — провал);
  6. асимметрия: потенциал/риск ≥ 1.5 (оба числа — из прецедента и стоп-условия);
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

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}\.US$")
MIN_DAYS, MAX_DAYS = 21, 91                  # 3–13 недель
MIN_FACTS, MIN_DOMAINS = 3, 2
MIN_ASYM = 1.5
P_MIN, P_MAX = 0.05, 0.95
MAX_PRICE_AGE_DAYS = 10
MIN_PRICE = 5.0                              # Ж1 разбора 02.11: копеечные бумаги — не идея дня
DIR_MAP = {"вверх": "above", "вниз": "below"}
REQUIRED = ("ticker", "name", "direction", "catalyst", "thesis", "facts", "precedent",
            "move_since_trigger_pct", "upside_pct", "downside_pct", "who_sells",
            "kill_conditions", "spec9")


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


def check(card, today=None):
    """Карточка → {'pass', 'fails', 'warnings', 'computed'}. Пустой fails = прошла шлагбаум."""
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
    good, domains = 0, set()
    for f in facts:
        if not isinstance(f, dict):
            continue
        url = str(f.get("url") or "")
        if url.startswith("http") and _date(f.get("date")) and str(f.get("text") or "").strip():
            good += 1
            domains.add(urlparse(url).netloc.lower().removeprefix("www."))
    comp["фактов_с_источником"], comp["источников"] = good, len(domains)
    if good < MIN_FACTS:
        fails.append(f"фактов с URL и датой {good} < {MIN_FACTS} (П8)")
    if len(domains) < MIN_DOMAINS:
        fails.append(f"разных источников {len(domains)} < {MIN_DOMAINS} (П8)")

    # 5. прецедент и «что уже в цене»
    prec = card["precedent"] if isinstance(card["precedent"], dict) else {}
    amp = str(prec.get("amplitude") or "")
    if not str(prec.get("url") or "").startswith("http"):
        fails.append("прецедент без URL (П8)")
    move = _num(card["move_since_trigger_pct"])
    ranges = PI.parse_amplitude(amp)
    far = max((max(abs(r["низ"]), abs(r["верх"])) for r in ranges), default=0.0)
    comp["амплитуда_дальний_край_пп"] = far
    if move is None:
        fails.append("ход с даты повода не числом — без него «не отыграно» не доказать")
    elif not ranges:
        fails.append("амплитуда прецедента не распознана числом")
    elif direction in DIR_MAP:
        # Ревью 02.11: доля «в цене» считается ПО НАПРАВЛЕНИЮ ИДЕИ — ход бумаги в сторону идеи против
        # модуля прецедентной амплитуды. Раньше знак прецедента (переменная механизма может идти
        # против бумаги) переворачивал проверку: отыгранная бумага проходила, свежая бракуется.
        r0 = ranges[0]
        lo, hi = sorted((abs(r0["низ"]), abs(r0["верх"])))
        in_dir = move if direction == "вверх" else -move
        pf = PI.played_fraction(in_dir, f"+{lo:g}–{hi:g}%" if lo != hi else f"+{hi:g}%")
        comp["что_в_цене"] = pf
        if "ОТКАЗ" in pf:
            fails.append("доля отыгранности не посчитана: " + pf["ОТКАЗ"])
        elif pf.get("вердикт") in (PI.В_ЦЕНЕ, PI.ПЕРЕЛЁТ):
            fails.append(f"звено уже отыграно числом: {pf['вердикт']} ({pf.get('строка')})")

    # 6. асимметрия
    up, dn = _num(card["upside_pct"]), _num(card["downside_pct"])
    if up is None or dn is None or up <= 0 or dn <= 0:
        fails.append("потенциал и риск обязаны быть положительными числами (% хода)")
    else:
        comp["асимметрия"] = round(up / dn, 2)
        if up / dn < MIN_ASYM:
            fails.append(f"асимметрия {up / dn:.2f} < {MIN_ASYM} (потенциал {up}% / риск {dn}%)")
        if far and up > far * 1.05:
            fails.append(f"потенциал {up}% больше прецедентного хода {far:g}% — питч без основы")

    # 8. §9-спека
    spec = card["spec9"] if isinstance(card["spec9"], dict) else {}
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
        hd = (rb - today).days
        comp["горизонт_дней"] = hd
        if not MIN_DAYS <= hd <= MAX_DAYS:
            fails.append(f"§9: горизонт {hd} дн вне 3–13 недель")
        if cd is not None and cd > rb:
            fails.append("§9: сверка раньше катализатора — вопрос разрешится до события")
    p = _num(spec.get("probability"))
    if p is None or not P_MIN <= p <= P_MAX:
        fails.append(f"§9: сырая вероятность вне [{P_MIN}..{P_MAX}] (для счёта, не для показа)")
    px, thr = _num(spec.get("last_close")), _num(spec.get("threshold"))
    pxd = _date(spec.get("last_close_date"))
    if px is None or px <= 0 or pxd is None:
        fails.append("§9: нет свежей цены закрытия с датой — порог не заякорить (§10)")
    else:
        if px < MIN_PRICE:
            fails.append(f"цена {px} < ${MIN_PRICE:g}: копеечная бумага не годится в идею дня")
        age = (today - pxd).days
        if age > MAX_PRICE_AGE_DAYS:
            fails.append(f"§9: цена закрытия устарела ({age} дн > {MAX_PRICE_AGE_DAYS})")
        if thr is not None:
            if not 0.5 * px <= thr <= 2.0 * px:
                fails.append(f"§9: порог {thr} не заякорен к цене {px} (вне 0.5×..2×)")
            sd = str(spec.get("direction") or "").lower()
            if (sd == "above" and thr <= px) or (sd == "below" and thr >= px):
                fails.append(f"§9: порог {thr} на уже-истинной стороне цены {px} — тривиальный вопрос")
            elif up is not None and up > 0:
                # цель прогноза — в пределах заявленного потенциала (иначе питч +40%, а печать — другое)
                thr_move = (thr / px - 1) * 100 if sd == "above" else (1 - thr / px) * 100
                comp["ход_до_порога_пп"] = round(thr_move, 2)
                if thr_move > up * 1.05:
                    fails.append(f"§9: порог требует хода {thr_move:.1f}% — больше заявленного потенциала {up}%")

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
    return {"pass": not fails, "fails": fails, "warnings": warns, "computed": comp}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Шлагбаум идеи охотника (жёсткие критерии кодом)")
    ap.add_argument("card")
    ap.add_argument("--today", default=None)
    a = ap.parse_args(argv)
    card = json.loads(pathlib.Path(a.card).read_text(encoding="utf-8"))
    res = check(card, today=_date(a.today) if a.today else None)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
