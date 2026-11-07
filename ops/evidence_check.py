# -*- coding: utf-8 -*-
"""ops/evidence_check.py — ПРОВЕРКА ДОКАЗАТЕЛЬСТВ карточки охотника КОДОМ (аудит 07.11).

Независимый аудит трёх идей (DHT, MP, FSLR) нашёл ошибки, которые прошли шлагбаум, суд и печать,
потому что шлагбаум проверял ФОРМУ (есть ли URL, сходятся ли проценты), а не СОДЕРЖАНИЕ доводов:

  №1 время события  — падение MP 05.11 названо реакцией на отчёт, вышедший ПОСЛЕ закрытия;
  №2 первоисточник  — у FSLR в фактах было исключение для старых контрактов, а тезис и стоп-условие
                      говорили «нельзя продать дешевле $0.38»; у MP «поправки нет» держалось на
                      вторичных сайтах, первоисточник был закрыт сетью;
  №3 показатели     — EPS без базиса (GAAP / скорректированный), сравнение с консенсусом без сверки
                      базиса, разовая продажа в выручке;
  №4 цель           — исторический уровень и чужой таргет вместо собственного расчёта;
  №7 вероятность    — субъективная оценка не помечена как субъективная.

Каждое правило — детектор в тексте карточки + требование СТРУКТУРНОГО поля, которое закрывает
вопрос (цитата, исключения, проверка отсутствия, базис, расчёт, сессия). Нет поля — БРАК
(fails: в счёт не пишется, владелец видит идею «для обсуждения» с причиной). Слабое, но честное
обоснование — ОГОВОРКА (labels: код дописывает её в пуш; модель не может её убрать).

Схема новых полей — knowledge/idea_bar.yaml (раздел evidence) и .claude/workflows/hunt-idea.js.
"""
import datetime
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mathlib import event_timing as ET    # noqa: E402
from mathlib import target_calc as TC     # noqa: E402
from mathlib import expectations as EX    # noqa: E402
from mathlib import materiality as MAT    # noqa: E402

# ── детекторы ────────────────────────────────────────────────────────────────
REGULATORY_RE = re.compile(
    r"указ|прокламац|раздел\w*\s*232|section\s*232|пошлин|тариф|tariff|federal register|"
    r"объявлени\w*\s+(?:минкоммерции|мофкома|№)|экспортн\w*\s+(?:ограничени|контрол)|"
    r"санкци|постановлени|регулятор\w*\s+(?:решени|срок)|\bfda\b|\bpdufa\b", re.I)
EXCEPTION_RE = re.compile(
    r"исключени|за исключением|не распространя|освобожд\w+ от|\bexcept|exempt|grandfather|"
    r"переходн\w+ (?:период|положени)|ранее заключ|подписанн\w* до|заключ\w+ до", re.I)
ABSOLUTE_RE = re.compile(r"\bнельзя\b|невозможно|никто не (?:может|сможет)|впервые|\bcannot\b", re.I)
EARNINGS_RE = re.compile(r"отч[её]т\w*\s+(?:\w+\s+){0,2}за\s|квартальн\w+\s+отч[её]т|\bearnings\b|"
                         r"\bresults\b|после закрытия биржи|до открытия биржи", re.I)
_ABS_OBJ = r"(?:поправк|документ|текст|уведомлени|объявлени|решени|продлени|перенос|отмен|запрет|правил)"
ABSENCE_RE = re.compile(
    r"(?:\bнет\b|не было|не найден\w*|не опубликован\w*|не обнаружен\w*|не вижу|отсутству\w*)\s[^.;]{0,40}?"
    + _ABS_OBJ + r"|" + _ABS_OBJ + r"\w*[^.;]{0,80}?\s(?:\bнет\b|не найден\w*|не опубликован\w*|не обнаружен\w*)"
    r"|\bno (?:amendment|notice|extension)", re.I)
EPS_RE = re.compile(r"на акцию|\beps\b|per share", re.I)
CONSENSUS_RE = re.compile(r"консенсус|consensus|ожидани\w* аналитик", re.I)
REVENUE_RE = re.compile(r"выручк\w*\s[^.;]{0,30}\$\s?\d|\brevenue\b", re.I)
CAUSAL_MOVE_RE = re.compile(
    r"(?:ответил\w*|отреагировал\w*)\s[^.;]{0,40}(?:падени|рост|снижени)|"
    r"\bна\s+(?:отличн\w+\s+|хорош\w+\s+|слаб\w+\s+|плох\w+\s+)?(?:отч[её]те|новост\w+|проекте|объявлени\w+)\b"
    r"[^.;]{0,80}?[−\-+]\s?\d", re.I)

BASES = {"GAAP", "non-GAAP", "adjusted", "n/a"}
SAME_BASIS = {"non-GAAP": "adj", "adjusted": "adj", "GAAP": "gaap", "n/a": "na"}
SESSIONS = set(ET.SESSIONS)
PROB_KINDS = {"subjective", "calibrated"}


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _http(u):
    return str(u or "").startswith("http")


def _snip(text, m, n=90):
    s = max(0, m.start() - 30)
    return "«" + " ".join(str(text)[s:s + n].split()) + "…»"


def _texts(card, keys):
    out = []
    for k in keys:
        v = card.get(k)
        if isinstance(v, dict):
            v = v.get("what") or v.get("text")
        if v:
            out.append((k, str(v)))
    return out


# ── правила ──────────────────────────────────────────────────────────────────
def check_regulatory(card, fails, labels):
    cat = card.get("catalyst") if isinstance(card.get("catalyst"), dict) else {}
    is_reg = str(cat.get("type") or "").lower() == "regulatory" or bool(REGULATORY_RE.search(str(cat.get("what") or "")))
    if not is_reg:
        return False
    reg = card.get("regulatory") if isinstance(card.get("regulatory"), dict) else None
    if not reg:
        fails.append("регуляторный катализатор без проверки полного текста документа (поле regulatory: "
                     "ссылка, цитата, дата вступления, область действия, исключения, переходные условия, "
                     "проверка поправок)")
        reg = {}
    else:
        if not _http(reg.get("document_url")):
            fails.append("regulatory: нет ссылки на сам документ (первоисточник)")
        if len(str(reg.get("quote") or "").strip()) < 20:
            fails.append("regulatory: нет подтверждающего фрагмента документа (цитаты)")
        if not _date(reg.get("effective_date")):
            fails.append("regulatory: нет даты вступления в силу")
        if not str(reg.get("scope") or "").strip():
            fails.append("regulatory: не указана область действия (кого и что касается)")
        am = reg.get("amendments_checked") if isinstance(reg.get("amendments_checked"), dict) else {}
        if not (_date(am.get("checked_at")) and [s for s in (am.get("sources") or []) if _http(s)]):
            fails.append("regulatory: не проверены изменения документа (дата проверки и источники)")
        if not str(reg.get("transition") or "").strip():
            fails.append("regulatory: не описаны переходные условия (или «нет» с цитатой)")
    excs = [e for e in (reg.get("exceptions") or []) if isinstance(e, dict) and str(e.get("text") or "").strip()]
    if reg and not excs and len(str(reg.get("no_exceptions_quote") or "").strip()) < 20:
        fails.append("regulatory: исключения не проверены — нужен список исключений или цитата, что их нет")
    # противоречие: исключение встречается в фактах/документе, а в разборе исключений его нет
    if not excs:
        pool = [("catalyst", str(cat.get("what") or ""))] + [
            (f"факт {i + 1}", str(f.get("text") or "")) for i, f in enumerate(card.get("facts") or [])
            if isinstance(f, dict)] + [("regulatory.quote", str(reg.get("quote") or ""))]
        for where, t in pool:
            m = EXCEPTION_RE.search(t)
            if m:
                fails.append(f"в источниках есть исключение из правила ({where}: {_snip(t, m)}), а тезис и "
                             "стоп-условия его не учитывают — разбери его влияние на скорость эффекта и на условия отмены")
                break
    for e in excs:
        if not str(e.get("effect") or "").strip():
            fails.append(f"исключение «{str(e.get('text'))[:60]}» без оценки влияния на тезис и сроки")
        if not str(e.get("kill_review") or "").strip():
            fails.append(f"исключение «{str(e.get('text'))[:60]}» не сверено со стоп-условиями "
                         "(иначе условие отмены может сработать на законном исключении)")
    if excs:
        th = str(card.get("thesis") or "")
        m = ABSOLUTE_RE.search(th)
        if m and not EXCEPTION_RE.search(th):
            fails.append(f"тезис сформулирован абсолютно ({_snip(th, m)}), а у правила есть исключения — "
                         "оговори их в тезисе")
    return True


def check_absence(card, fails, labels):
    hits = []
    for k, t in _texts(card, ("catalyst", "thesis", "why_not_priced", "entry")):
        m = ABSENCE_RE.search(t)
        if m:
            hits.append(f"{k}: {_snip(t, m)}")
    for i, f in enumerate(card.get("facts") or []):
        if isinstance(f, dict):
            m = ABSENCE_RE.search(str(f.get("text") or ""))
            if m:
                hits.append(f"факт {i + 1}: {_snip(f['text'], m)}")
    if not hits:
        return
    checks = [c for c in (card.get("absence_checks") or []) if isinstance(c, dict)]
    good = [c for c in checks if str(c.get("claim") or "").strip() and _date(c.get("checked_at"))
            and [s for s in (c.get("sources_checked") or []) if _http(s)]]
    if not good:
        fails.append("утверждение об ОТСУТСТВИИ документа или события без записи проверки "
                     f"(absence_checks: что искали, где, когда, смотрели ли первоисточник): {hits[0]}. "
                     "«Не нашёл» — не доказательство «нет»")
        return
    if not any(c.get("primary_checked") is True for c in good):
        labels.append("Отсутствие нужного документа проверено только по вторичным источникам — первоисточник "
                      "напрямую не открывался, поэтому «документа нет» здесь означает «не найден»")


def check_metrics(card, fails, labels):
    for i, f in enumerate(card.get("facts") or []):
        if not isinstance(f, dict):
            continue
        t = str(f.get("text") or "")
        eps, cons, rev = EPS_RE.search(t), CONSENSUS_RE.search(t), REVENUE_RE.search(t)
        if not (eps or cons or rev):
            continue
        m = f.get("metric") if isinstance(f.get("metric"), dict) else None
        tag = f"факт {i + 1}"
        if not m:
            fails.append(f"{tag}: финансовый показатель без определения (metric: имя, период, единицы, "
                         f"базис GAAP/скорректированный): {_snip(t, eps or cons or rev)}")
            continue
        basis = str(m.get("basis") or "")
        if basis not in BASES:
            fails.append(f"{tag}: не указан базис показателя (GAAP / non-GAAP / adjusted / n/a)")
        if not str(m.get("period") or "").strip():
            fails.append(f"{tag}: не указан период показателя")
        if cons:
            c = m.get("consensus") if isinstance(m.get("consensus"), dict) else {}
            cb = str(c.get("basis") or "")
            if not cb:
                fails.append(f"{tag}: сравнение с консенсусом без базиса консенсуса")
            elif SAME_BASIS.get(cb, cb) != SAME_BASIS.get(basis, basis):
                fails.append(f"{tag}: факт ({basis}) сравнивается с консенсусом другого базиса ({cb}) — "
                             "сравнение несопоставимо")
        if rev and m.get("one_offs_checked") is not True:
            fails.append(f"{tag}: выручка без проверки разовых статей (one_offs_checked; разовые — в one_offs "
                         "с суммой и повторяемостью)")
        for o in m.get("one_offs") or []:
            if isinstance(o, dict) and o.get("recurring") is None:
                fails.append(f"{tag}: разовая статья «{str(o.get('item'))[:40]}» без оценки повторяемости")


def check_event_timing(card, fails, labels):
    cat = card.get("catalyst") if isinstance(card.get("catalyst"), dict) else {}
    is_earn = str(cat.get("type") or "").lower() == "earnings" or bool(EARNINGS_RE.search(str(cat.get("what") or "")))
    if not is_earn:
        return
    ts, ses = cat.get("datetime_utc"), str(cat.get("session") or "")
    if not ts or ses not in SESSIONS:
        fails.append("катализатор-отчёт без времени публикации в UTC и торговой сессии "
                     "(catalyst.datetime_utc, catalyst.session) — нельзя отличить ход до события от реакции")
        return
    try:
        ok, got = ET.session_matches(ses, ts)
    except ValueError as e:
        fails.append(f"время катализатора не разобрано: {e}")
        return
    if not ok:
        fails.append(f"сессия катализатора указана «{ses}», а по времени {ts} это «{got}»")


def check_price_causality(card, fails, labels):
    moves = [m for m in (card.get("price_moves") or []) if isinstance(m, dict)]
    for k, t in _texts(card, ("thesis", "why_not_priced", "who_sells")):
        m = CAUSAL_MOVE_RE.search(t)
        if not m:
            continue
        if not moves:
            fails.append(f"{k}: причина хода цены приписана событию без пометки «гипотеза/подтверждено» "
                         f"(price_moves): {_snip(t, m)}")
            return
    for mv in moves:
        c = str(mv.get("causality") or "")
        if c not in ("hypothesis", "confirmed"):
            fails.append(f"price_moves {mv.get('date')}: причинность не помечена (hypothesis/confirmed)")
        elif c == "confirmed" and not _http(mv.get("source")):
            fails.append(f"price_moves {mv.get('date')}: «подтверждено» без источника")
        if mv.get("event_utc"):
            try:
                ET.classify(mv["event_utc"])
            except ValueError as e:
                fails.append(f"price_moves {mv.get('date')}: время события не разобрано: {e}")


def check_target(card, fails, labels):
    r = TC.check_basis(card.get("target_basis"), card.get("target_price"))
    fails += [f"цель: {x}" for x in r["fails"]]
    if r["status"] != "обоснована":
        labels.append("Цель предварительная: " + "; ".join(r["reasons"][:2]) + " — идея требует исследования")
    return r


def check_probability(card, fails, labels):
    s9 = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    k = str(s9.get("probability_kind") or "")
    if k not in PROB_KINDS:
        fails.append("§9: не помечено, чья вероятность — субъективная оценка автора или калиброванная "
                     "(spec9.probability_kind)")
    elif k == "calibrated" and not str(s9.get("calibration_ref") or "").strip():
        fails.append("§9: вероятность названа калиброванной без ссылки на калибровку (calibration_ref)")


# ── VQ-2 (владелец 07.11): «рынок ждёт X, мы ждём Y», существенность, устойчивость, глубина цепочки ──
MARKET_KINDS = {"magnitude", "timing", "flow", "rule"}
MIN_GAP_REL = 0.05          # наш сценарий отличается от рынка меньше чем на 5% — расхождения нет
MAX_CHAIN = 3               # звеньев от повода до бумаги (idea_bar: «2–3 звена»)
MAX_HYPOTHESIS_LINKS = 1    # жёсткая остановка: не больше одного неподтверждённого звена


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_market_view(card, fails, labels):
    """Главный вопрос инвестора: что уже заложено в цену и почему мы ждём другого. Нет ответа с цифрами и
    источником ожидания рынка — брак (идея уходит владельцу «для обсуждения», в счёт не пишется)."""
    mv = card.get("market_view") if isinstance(card.get("market_view"), dict) else None
    if not mv:
        fails.append("рынок/мы: не показано, что рынок уже ждёт и чем отличается наш сценарий (market_view: "
                     "показатель, ожидание рынка с источником, наше значение, почему иначе)")
        return None
    kind = str(mv.get("kind") or "magnitude")
    m = mv.get("market") if isinstance(mv.get("market"), dict) else {}
    o = mv.get("ours") if isinstance(mv.get("ours"), dict) else {}
    why = str(mv.get("why_different") or "").strip()
    out = {"kind": kind}
    if kind not in MARKET_KINDS:
        fails.append(f"рынок/мы: неизвестный вид расхождения {kind!r} (magnitude | timing | flow | rule)")
        return out
    if len(why) < 40:
        fails.append("рынок/мы: не объяснено, почему мы ждём другого (why_different ≥ 40 знаков)")
    if not str(m.get("source") or "").strip():
        fails.append("рынок/мы: у ожидания рынка нет источника (консенсус EODHD, расчёт по цене, опционы…)")
    if kind == "magnitude":
        mv_, ov = m.get("value"), o.get("value")
        if not (_num(mv_) and _num(ov)):
            fails.append("рынок/мы: нет чисел — ожидание рынка и наше значение одного показателя (market.value, ours.value)")
            return out
        rel = (ov - mv_) / abs(mv_) if mv_ else None
        out["разрыв_отн"] = None if rel is None else round(rel, 4)
        if rel is not None and abs(rel) < MIN_GAP_REL:
            fails.append(f"рынок/мы: наш сценарий почти совпадает с рынком ({rel * 100:+.1f}%) — "
                         f"идее нечем отличаться от уже заложенного в цену")
    elif kind == "timing":
        if not (_date(m.get("date")) and _date(o.get("date"))) or str(m.get("date")) == str(o.get("date")):
            fails.append("рынок/мы: расхождение по срокам без двух разных дат (market.date — когда ждёт рынок, ours.date — мы)")
    elif kind in ("flow", "rule"):
        labels.append("Расхождение с рынком — не в цифрах ожиданий, а в " +
                      ("вынужденных продажах" if kind == "flow" else "понимании правила") +
                      ": цифрами не проверено")
    imp = mv.get("implied") if isinstance(mv.get("implied"), dict) else None
    if imp:
        r = EX.implied_growth(imp.get("market_cap"), imp.get("base_earnings"), r=imp.get("r", EX.DEFAULT_R),
                              g=imp.get("g", EX.DEFAULT_G), years=int(imp.get("years", EX.DEFAULT_YEARS)))
        out["зашито_в_цену"] = r
        if r.get("ok") and kind == "magnitude" and str(mv.get("metric") or "").lower().startswith("рост"):
            if _num(m.get("value")) and abs(m["value"] - r["рост_в_год"]) > 0.02:
                fails.append(f"рынок/мы: заявленный рост, зашитый в цену ({m['value']:.3f}), расходится с "
                             f"пересчётом кодом ({r['рост_в_год']:.3f}) больше чем на 2 п.п.")
    return out


def check_materiality(card, fails, labels):
    mt = card.get("materiality") if isinstance(card.get("materiality"), dict) else None
    if not mt:
        labels.append("Существенность эффекта для компании не посчитана")
        return None
    r = MAT.materiality(effect_profit_per_year=mt.get("effect_profit_per_year"),
                        effect_revenue_per_year=mt.get("effect_revenue_per_year"), years=mt.get("years"),
                        net_income=mt.get("net_income"), revenue=mt.get("revenue"), market_cap=mt.get("market_cap"))
    if not r.get("ok"):
        labels.append("Существенность эффекта не посчитана: " + str(r.get("причина")))
    elif r["вердикт"] == MAT.ШУМ:
        fails.append(f"существенность: эффект — шум для размера компании ({r['основание']}: "
                     f"{r['мерила'][r['основание']]})")
    elif r["вердикт"] == MAT.ПОГРАНИЧНЫЙ:
        labels.append("Эффект для компании пограничный по размеру")
    if r.get("оговорка"):
        labels.append(r["оговорка"])
    return r


def check_durability(card, fails, labels):
    d = card.get("durability") if isinstance(card.get("durability"), dict) else None
    if not d or len(str(d.get("reason") or "").strip()) < 20 or not _http(d.get("url")):
        labels.append("Устойчивость эффекта не обоснована проверяемым фактом (что помешает конкурентам "
                      "быстро отнять выгоду) — идея предварительная")


def check_chain(card, fails, labels):
    """Жёсткая остановка глубины: 452 исторические пары «событие → звено» — 0 подтверждены сверх случайности,
    поэтому дальше третьего звена и больше одного неподтверждённого звена не идём."""
    ch = card.get("chain")
    if not isinstance(ch, list) or not ch:
        fails.append("цепочка: не показаны звенья от повода до бумаги с качеством каждой связи (chain)")
        return
    if len(ch) > MAX_CHAIN:
        fails.append(f"цепочка: {len(ch)} звеньев — дальше {MAX_CHAIN} связь слишком слабая (жёсткий предел)")
    hyp = [i for i, x in enumerate(ch) if not isinstance(x, dict) or x.get("quality") != "confirmed"
           or not _http(x.get("url"))]
    if len(hyp) > MAX_HYPOTHESIS_LINKS:
        fails.append(f"цепочка: неподтверждённых звеньев {len(hyp)} — держится на гипотезе поверх гипотезы "
                     f"(допустимо не больше {MAX_HYPOTHESIS_LINKS}, подтверждение — источник)")
    elif hyp:
        labels.append("Одно звено цепочки — гипотеза без подтверждения источником")


def check(card):
    """Карточка → {'fails', 'labels', 'target', 'regulatory'}. Пустой fails = доказательства в порядке."""
    fails, labels = [], []
    if not isinstance(card, dict):
        return {"fails": ["карточка не объект"], "labels": [], "target": None, "regulatory": False}
    reg = check_regulatory(card, fails, labels)
    check_absence(card, fails, labels)
    check_metrics(card, fails, labels)
    check_event_timing(card, fails, labels)
    check_price_causality(card, fails, labels)
    tgt = check_target(card, fails, labels)
    check_probability(card, fails, labels)
    mv = check_market_view(card, fails, labels)
    mat = check_materiality(card, fails, labels)
    check_durability(card, fails, labels)
    check_chain(card, fails, labels)
    return {"fails": fails, "labels": labels, "target": tgt, "regulatory": reg, "market_view": mv,
            "materiality": mat}


CAVEATS_HEAD = "Оговорки проверки."


def caveats_paragraph(card):
    """Абзац, который пишет КОД (не модель) и модель не может убрать: оговорки доказательной базы."""
    labs = check(card)["labels"]
    if not labs:
        return None
    return CAVEATS_HEAD + " " + " ".join(x.rstrip(".") + "." for x in labs)


if __name__ == "__main__":
    import json
    c = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(check(c), ensure_ascii=False, indent=1, default=str))
