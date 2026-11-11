# -*- coding: utf-8 -*-
"""ops/chat_seal.py — итоговый кандидат разговора «найди идею» → §9-прогноз в счёт ДО сделки (Q-чат-в-счёт, 11.11).

ПОЧЕМУ (испытатель 10.11): главная поверхность продукта теперь — разговор в Telegram. За неделю — 10+ кандидатов,
09.11 итог «EQT», обсуждение покупки 4 акций, а прогноза в журнале нет: умение метода не измеряется. Вердикт об
умении (точка Б, N≥40) — только по сверенным прогнозам, записанным заранее (П16).

ЧТО: отдельный герметичный трек kind=chat_idea (свой счёт; к §11 и к счёту охотника не приближается).
Прогноз записывает КОД по проверяемым правилам §9:
  • бумага с историей цен в снимке сервера (якорь — последнее закрытие, сверка — закрытие EODHD);
  • направление above/below, порог — на ещё НЕ истинной стороне цены и в пределах ×0.5–×2 от неё;
  • срок сверки 3–13 недель (зона, где прогнозы проверяемы); ход до порога ≥ 0.5 обычной качки за срок —
    иначе это ставка на шум;
  • база шума — вероятность того же исхода от одной качки цены (mathlib/base_odds) — счёт метода считается
    против неё;
  • вероятность — по желанию, только субъективная оценка с основанием (без неё прогноз всё равно сверяется:
    «сбылось k из n при ожидании шума X»);
  • по бумаге открыт не больше одного прогноза трека (коррелированные исходы — не независимые).
Печать в БОЕВОЙ журнал — только на сервере (как у охотника). Исход сверяет общий orchestrator/resolve.py.
"""
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

KIND = "chat_idea"
MIN_WEEKS, MAX_WEEKS = 3, 13
MIN_SIGMA_MOVE = 0.5
DEDUP_FIELDS = ("track", "asset", "direction", "threshold", "resolve_by")


def _read(path):
    import json
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def build_spec(ticker, direction, threshold, resolve_by, *, probability=None, probability_basis="", thesis="",
               search_id=None, sources=None, today=None, snapshot=None):
    """→ (spec, None) или (None, причина отказа словами). Ничего не пишет."""
    import math
    from mathlib import base_odds as BO
    try:
        from ops import price_snapshot as PS
    except ImportError:
        import price_snapshot as PS
    today = datetime.date.fromisoformat(str(today)[:10]) if today else datetime.datetime.now(
        datetime.timezone.utc).date()
    sym = str(ticker or "").upper().strip()
    sym = sym if "." in sym else sym + ".US"
    direction = str(direction or "").lower()
    if direction not in ("above", "below"):
        return None, "направление — above (закроется выше порога) или below (ниже)"
    try:
        thr = float(threshold)
        rb = datetime.date.fromisoformat(str(resolve_by)[:10])
    except (TypeError, ValueError):
        return None, "порог — число, срок — дата ГГГГ-ММ-ДД"
    weeks = (rb - today).days / 7.0
    if not (MIN_WEEKS <= weeks <= MAX_WEEKS + 0.5):
        return None, f"срок {rb} — {weeks:.1f} недель; проверяемая зона {MIN_WEEKS}–{MAX_WEEKS} недель"
    snap = snapshot if snapshot is not None else PS.lookup(sym)
    if snap.get("ошибка"):
        return None, f"нет истории цен {sym} в снимке сервера — сверять нечем (§9): {snap['ошибка']}"
    px, last_d, sig = snap.get("last_close"), snap.get("last_date"), snap.get("sigma_daily")
    if not px or not sig:
        return None, f"нет цены или колебаний {sym} — прогноз не измерим против шума"
    if not (0.5 * px <= thr <= 2.0 * px):
        return None, f"порог {thr} не заякорен к цене ~{px:.2f} (допустимо ×0.5–×2)"
    if (direction == "above" and thr <= px) or (direction == "below" and thr >= px):
        return None, f"порог {thr} уже на истинной стороне цены ~{px:.2f} — это не прогноз"
    days = BO.trading_days(last_d, rb.isoformat())
    z = math.log(thr / px) / (sig * math.sqrt(days)) if days else 0
    if abs(z) < MIN_SIGMA_MOVE:
        return None, (f"ход до порога — {abs(z):.2f} обычной качки за срок (< {MIN_SIGMA_MOVE}): ставка на шум, "
                      "а не на взгляд")
    p = None
    if probability is not None:
        try:
            p = float(probability)
        except (TypeError, ValueError):
            return None, "вероятность — число от 0 до 1 или не указывать"
        if not (0.02 <= p <= 0.98):
            return None, "вероятность вне 2–98% — так уверенно не бывает; не указывай или поправь"
        if len(str(probability_basis or "")) < 20:
            return None, "у вероятности нет основания (≥20 знаков) — или объясни, или не указывай"
    base = BO.base_probability(px, thr, sig, days, direction)
    spec = {
        "kind": KIND, "track": KIND, "asset": sym, "direction": direction, "threshold": round(thr, 4),
        "resolve_by": rb.isoformat() + "T20:00:00+00:00", "price_source": f"EODHD close {sym}",
        "probability": p, "probability_kind": "subjective" if p is not None else None,
        "вероятность_основание": str(probability_basis or "")[:400] if p is not None else None,
        "вероятность_база": round(base, 4) if base is not None else None,
        "якорь_цена": px, "якорь_дата": last_d, "ход_проц": round((thr / px - 1) * 100, 2), "ход_в_сигмах": round(z, 3),
        "тезис": str(thesis or "")[:800], "поиск": search_id, "источники": list(sources or [])[:10],
        "источник": "разговор «найди идею» в Telegram (агент), запись кодом ops/chat_seal",
        "spec_ref": "Q-чат-в-счёт 11.11; §9 разрешимость; П16 форвард-онли",
    }
    return spec, None


def seal(spec, predictions_path=None, today=None):
    """Запечатать (одна открытая запись трека на бумагу). → (record|None, причина|None)."""
    from mathlib import sealing as SEAL
    try:
        from ops import hunter_seal as HS
    except ImportError:
        import hunter_seal as HS
    target = pathlib.Path(predictions_path or HS.BATTLE_PRED)
    if not HS._battle_write_allowed(target):
        return None, "запись в боевой журнал — только на сервере"
    today = str(today or datetime.datetime.now(datetime.timezone.utc).date())[:10]
    open_same = [r for r in _read(target) if r.get("kind") == KIND and r.get("asset") == spec["asset"]
                 and str(r.get("resolve_by") or "")[:10] >= today]
    if open_same:
        return None, (f"по {spec['asset']} уже идёт прогноз разговора до {str(open_same[-1]['resolve_by'])[:10]} — "
                      "второй до его сверки не открываю")
    try:
        rec = SEAL.seal(spec, path=target, dedup_fields=DEDUP_FIELDS)
    except ValueError as e:
        return None, str(e)
    if rec is None:
        return None, "такая ставка уже записана"
    return rec, None


def words(rec):
    """Строка владельцу — пишет КОД: что записано в счёт и как будет сверено."""
    side = "выше" if rec["direction"] == "above" else "ниже"
    s = (f"Записал в счёт ДО сделки: {rec['asset']} закроется {side} ${rec['threshold']:.2f} к "
         f"{rec['resolve_by'][:10]} (сейчас ${rec['якорь_цена']:.2f}, ход {rec['ход_проц']:+.1f}%)")
    if rec.get("вероятность_база") is not None:
        s += f"; от одной качки цены такое бывает примерно в {rec['вероятность_база'] * 100:.0f}% случаев"
    if rec.get("probability") is not None:
        s += f", моя оценка — {rec['probability'] * 100:.0f}% (субъективная)"
    return s + ". Сверит код по закрытию, исход попадёт в счёт разговоров отдельно от охотника."


def from_worthy(h, search_id=None, today=None, snapshot=None):
    """Найденный кандидат очереди поиска (search_queue WORTHY) → спека: порог — базовый сценарий, срок — горизонт."""
    d = h.get("данные") or {}
    sc = d.get("сценарии") or {}
    base = (sc.get("базовый") or {}).get("цена")
    direction = "below" if str(d.get("направление") or "long") == "short" else "above"
    hz = d.get("горизонт")
    if not hz and d.get("горизонт_недель"):
        t = datetime.date.fromisoformat(str(today)[:10]) if today else datetime.date.today()
        hz = (t + datetime.timedelta(weeks=float(d["горизонт_недель"]))).isoformat()
    prob = d.get("вероятность") if d.get("основание_вероятности") else None
    return build_spec(h.get("тикер"), direction, base, hz, probability=prob,
                      probability_basis=d.get("основание_вероятности") or "", thesis=h.get("тезис"),
                      search_id=search_id, sources=[f.get("url") for f in (d.get("факты") or []) if isinstance(f, dict)],
                      today=today, snapshot=snapshot)
