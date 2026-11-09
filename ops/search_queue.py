# -*- coding: utf-8 -*-
"""ops/search_queue.py — поиск идеи доводится до результата (задание владельца 09.11, VQ-4 п.1–2).

ПОЧЕМУ: «найди идею» заканчивалась на первых отказах. Два слабых кандидата, отправленная карточка, «нечего»
или «ждём отчёта» записывались как закрытый запрос, хотя достойного кандидата не было. Теперь поиск —
сохраняемая очередь гипотез со статусом. Закончить его можно тремя способами:
  • найден достойный кандидат (found) — гипотеза прошла все этапы и проверку worthy_check кодом;
  • приостановлен с конкретной причиной (paused) — исчерпан разрешённый ресурс или ход ответа кончился;
    очередь и причина сохраняются, продолжение — только когда реально есть исполнитель;
  • отменён владельцем (cancelled).
Пока поиск идёт, статус — running. Других «успешных» концов нет.

Этапы гипотезы: генерация → быстрая_проверка → углубление → критика → оценка → результат.
Отказ гипотезы не завершает поиск. Вид отказа задаёт следующее направление:
  priced_in          прямой эффект уже в цене      → другие последствия и получатели прибыли (с проверкой существенности)
  mechanism_refuted  механизм опровергнут          → ветка закрыта, идём в другую
  instrument_unfit   инструмент не подходит        → другие способы выразить ту же идею
  no_data            не хватает данных             → добыть данные; остальные направления не ждут
Отвергнутый тезис нельзя вернуть под новым названием (отпечаток: бумага + слова механизма), если
не принесены новые существенные данные с источником.

Математика и правила — код, без LLM (Инв#6). Файл поиска: reports/ideas/searches/<id>.json.
"""
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

SEARCH_DIR = ROOT / "reports" / "ideas" / "searches"

RUNNING, FOUND, PAUSED, CANCELLED = "running", "found", "paused", "cancelled"
STATUS_RU = {RUNNING: "выполняется", FOUND: "найден достойный кандидат", PAUSED: "приостановлен",
             CANCELLED: "отменён владельцем"}
STAGES = ["генерация", "быстрая_проверка", "углубление", "критика", "оценка", "результат"]
OPEN, NEEDS_DATA, REJECTED, WORTHY = "open", "needs_data", "rejected", "worthy"
REJECT_KINDS = {
    "priced_in": "прямой эффект уже в цене → искать другие последствия и получателей прибыли (2-й/3-й круг), "
                 "проверяя экономическую значимость связи (materiality_check)",
    "mechanism_refuted": "механизм опровергнут → ветка закрыта, перейти к другому механизму или событию",
    "instrument_unfit": "инструмент не подходит → проверить другие способы выразить ту же идею "
                        "(другая бумага, поставщик, конкурент, фонд)",
    "no_data": "не хватает данных → добыть их (EODHD, отчётность, sec.gov, сайт компании); остальные направления не ждут",
}
# виды паузы: turn — кончился один ответ (продолжится на «продолжай»); budget — исчерпан бюджет поиска
# (повышать может только владелец); month — месячный лимит; queue_empty — направления исчерпаны
PAUSE_KINDS = {"turn", "budget", "month", "queue_empty", "executor"}
SEARCH_BUDGET_USD = 5.0     # суб-потолок одного поиска (строже месячного лимита; поднимает только владелец)
STALE_DAYS = 3              # факт или цена старше 3 дней — перепроверить перед выдачей
MIN_FACTS = 2
WEAK_BASIS_RE = re.compile(r"\bp/?e\b|низк\w* (?:мультипликатор|оценк)|акци\w* (?:уже )?(?:вырос|росл)|"
                           r"соотношени\w* цел\w*|цел\w* (?:к|и) стоп|модель счита|по мнению модели", re.I)
CEILING_RE = re.compile(r"историческ\w* максимум\w*[^.]{0,40}(?:потол|выше не|предел)", re.I)
_WORD = re.compile(r"[a-zа-яё0-9]{4,}", re.I)


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _http(u):
    return str(u or "").startswith(("http://", "https://"))


# ── хранение ────────────────────────────────────────────────────────────────────────
def _path(sid, d=None):
    return pathlib.Path(d or SEARCH_DIR) / f"{sid}.json"


def save(s, d=None):
    p = _path(s["id"], d)
    p.parent.mkdir(parents=True, exist_ok=True)
    s["обновлён"] = _now()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def load(sid, d=None):
    return json.loads(_path(sid, d).read_text(encoding="utf-8"))


def all_searches(d=None):
    out = []
    for p in sorted(pathlib.Path(d or SEARCH_DIR).glob("s_*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def active(executor=None, d=None):
    """Последний незавершённый поиск (running/paused) исполнителя — его продолжаем, а не начинаем заново."""
    rows = [s for s in all_searches(d) if s["статус"] in (RUNNING, PAUSED)
            and (executor is None or s.get("исполнитель") == executor)]
    return sorted(rows, key=lambda s: s.get("обновлён") or "")[-1] if rows else None


def _log(s, event, **kw):
    s.setdefault("журнал", []).append({"ts": _now(), "событие": event, **kw})


# ── поиск ───────────────────────────────────────────────────────────────────────────
def start(topic, executor, budget_usd=SEARCH_BUDGET_USD, d=None, sid=None):
    """Новый поиск. executor — кто реально его исполняет: 'telegram' (агент, только пока идёт ответ
    владельцу) или 'hunter' (облачная охота, только пока идёт её запуск). Фоновых исполнителей нет."""
    sid = sid or "s_" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    s = {"id": sid, "тема": str(topic or "")[:300], "исполнитель": executor, "создан": _now(),
         "статус": RUNNING, "пауза": None, "итог": None,
         "бюджет": {"лимит_usd": float(budget_usd), "потрачено_usd": 0.0},
         "гипотезы": [], "направления": [], "журнал": []}
    _log(s, "старт", тема=s["тема"])
    save(s, d)
    return s


def charge(s, usd):
    """Учесть фактическую стоимость. Бюджет поиска исчерпан → пауза (очередь цела, успех не засчитан)."""
    b = s["бюджет"]
    b["потрачено_usd"] = round(b["потрачено_usd"] + float(usd or 0), 4)
    if s["статус"] == RUNNING and b["потрачено_usd"] >= b["лимит_usd"]:
        pause(s, "budget", f"бюджет поиска исчерпан: потрачено ${b['потрачено_usd']:.2f} из ${b['лимит_usd']:.2f}; "
                           f"поднять может только владелец")
    return s


def pause(s, kind, reason):
    if kind not in PAUSE_KINDS:
        raise ValueError(f"вид паузы {kind!r} не из {sorted(PAUSE_KINDS)}")
    if s["статус"] in (FOUND, CANCELLED):
        return s
    s["статус"] = PAUSED
    s["пауза"] = {"вид": kind, "причина": str(reason)[:500], "когда": _now(),
                  "очередь": queue_summary(s)}
    _log(s, "пауза", вид=kind, причина=reason)
    return s


def resume(s, by="владелец"):
    """Продолжить приостановленный поиск. Паузу по бюджету снимает только владелец, подняв бюджет."""
    if s["статус"] != PAUSED:
        return s, None
    p = s.get("пауза") or {}
    if p.get("вид") in ("budget", "month") and s["бюджет"]["потрачено_usd"] >= s["бюджет"]["лимит_usd"]:
        return s, "бюджет поиска исчерпан — продолжить можно только после решения владельца о бюджете"
    s["статус"], s["пауза"] = RUNNING, None
    _log(s, "продолжение", кто=by)
    return s, None


def raise_budget(s, new_limit_usd, owner_words):
    """Поднять бюджет поиска — ТОЛЬКО со словами владельца (не автоматически)."""
    if not str(owner_words or "").strip():
        raise ValueError("бюджет поиска поднимает только владелец — нужна его фраза")
    s["бюджет"]["лимит_usd"] = float(new_limit_usd)
    _log(s, "бюджет_поднят", лимит=new_limit_usd, слова_владельца=str(owner_words)[:200])
    return s


def cancel(s, owner_words):
    if not str(owner_words or "").strip():
        raise ValueError("отменить поиск может только владелец — нужна его фраза")
    s["статус"], s["пауза"] = CANCELLED, None
    _log(s, "отмена", слова_владельца=str(owner_words)[:200])
    return s


# ── гипотезы ────────────────────────────────────────────────────────────────────────
def fingerprint(ticker, mechanism):
    words = sorted({w.lower() for w in _WORD.findall(str(mechanism or ""))})
    return {"тикер": str(ticker or "").upper().split(".")[0], "слова": words}


def _similar(fp1, fp2, thr=0.5):
    if not fp1["тикер"] or fp1["тикер"] != fp2["тикер"]:
        return False
    a, b = set(fp1["слова"]), set(fp2["слова"])
    if not a or not b:
        return True                       # та же бумага без описания механизма — считаем тем же тезисом
    return len(a & b) / len(a | b) >= thr


def _new_data_ok(nd):
    return isinstance(nd, list) and any(isinstance(x, dict) and _http(x.get("url")) and _date(x.get("опубликовано"))
                                        and len(str(x.get("текст") or "")) >= 20 for x in nd)


def add_hypothesis(s, thesis, ticker, mechanism, beneficiary="", circle=1, direction_id=None, new_data=None):
    """→ (гипотеза | None, ошибка | None). Отвергнутый тезис под новым названием — отказ без новых данных."""
    if len(str(thesis or "")) < 20 or not str(ticker or "").strip() or len(str(mechanism or "")) < 10:
        return None, "нужны тезис (≥20 знаков), тикер и механизм (≥10 знаков)"
    fp = fingerprint(ticker, mechanism)
    for h in s["гипотезы"]:
        if _similar(fp, h["отпечаток"]):
            if h["вердикт"] == REJECTED and not _new_data_ok(new_data):
                return None, (f"это уже отвергнутый тезис {h['id']} ({h['тикер']}: {h['отказ']['вид']} — "
                              f"{h['отказ']['причина'][:120]}). Вернуть можно только с НОВЫМИ существенными данными "
                              f"(new_data: факт ≥20 знаков, url, дата публикации)")
            if h["вердикт"] != REJECTED:
                return None, f"такая гипотеза уже в очереди: {h['id']} ({h['тикер']}, этап {h['этап']})"
    h = {"id": f"h{len(s['гипотезы']) + 1}", "тезис": str(thesis)[:600], "тикер": fp["тикер"],
         "механизм": str(mechanism)[:400], "получатель": str(beneficiary or "")[:200], "круг": int(circle or 1),
         "отпечаток": fp, "этап": STAGES[0], "вердикт": OPEN, "отказ": None, "нужно_данных": [],
         "направление": direction_id, "новые_данные": new_data or None, "данные": {}, "история": []}
    s["гипотезы"].append(h)
    if direction_id:
        for dr in s["направления"]:
            if dr["id"] == direction_id:
                dr["взято"] = True
    _log(s, "гипотеза", id=h["id"], тикер=h["тикер"])
    return h, None


def _hyp(s, hid):
    for h in s["гипотезы"]:
        if h["id"] == hid:
            return h
    return None


def _add_direction(s, kind, from_h, text):
    dr = {"id": f"d{len(s['направления']) + 1}", "вид": kind, "от_гипотезы": from_h["id"],
          "текст": text, "взято": False}
    s["направления"].append(dr)
    return dr


# ── требования этапов ───────────────────────────────────────────────────────────────
def quick_check_gaps(data):
    """Быстрая проверка до длинного разбора: факты, механизм, цена, потенциал, риски, инструмент."""
    g = []
    facts = data.get("факты") or []
    if not any(isinstance(f, dict) and _http(f.get("url")) for f in facts):
        g.append("ключевой факт с источником (url)")
    if len(str(data.get("механизм_проверен") or "")) < 20:
        g.append("причинный механизм: что именно и через что меняет прибыль компании")
    if not _num(data.get("цена")) or not _date(data.get("дата_цены")):
        g.append("текущая цена с датой (price_snapshot / EODHD)")
    if not _num(data.get("потенциал_пп")):
        g.append("оставшийся потенциал от текущей цены, %")
    if not data.get("риски"):
        g.append("существенные риски")
    if data.get("инструмент_доступен") is not True:
        g.append("доступность инструмента (торгуется, ликвидность)")
    return g


def worthy_gaps(h, today=None):
    """Достоин ли кандидат выдачи. Пустой список — достоин. Каждый пункт — чего не хватает, словами."""
    today = _date(today or _today())
    d = h.get("данные") or {}
    g = []
    # 1) проверенные опорные факты: источник, дата публикации, дата проверки (свежая)
    facts = [f for f in (d.get("факты") or []) if isinstance(f, dict)]
    good = [f for f in facts if _http(f.get("url")) and _date(f.get("опубликовано")) and _date(f.get("проверено"))]
    if len(good) < MIN_FACTS:
        g.append(f"нужно ≥{MIN_FACTS} опорных фактов с источником, датой публикации и датой проверки")
    stale = [f for f in good if (today - _date(f["проверено"])).days > STALE_DAYS]
    if stale:
        g.append(f"факты проверены давно (>{STALE_DAYS} дн.): перепроверить {len(stale)} шт.")
    # 2) что рынок недооценивает и на чём основан вывод (те же правила, что у карточки охотника)
    from ops import evidence_check as EC
    fails, labels = [], []
    EC.check_market_view({"market_view": d.get("market_view")}, fails, labels)
    g += fails
    mv = d.get("market_view") if isinstance(d.get("market_view"), dict) else {}
    basis = mv.get("основано_на") or []
    if not basis:
        g.append("вывод «рынок недооценивает» не привязан к проверенным фактам (market_view.основано_на)")
    if WEAK_BASIS_RE.search(str(mv.get("why_different") or "")) and len(basis) < 1:
        g.append("преимущество держится на прошлом росте / низком P/E / цели к стопу / мнении модели — это не довод")
    if d.get("materiality"):
        f2 = []
        EC.check_materiality({"materiality": d.get("materiality")}, f2, [])
        g += f2
    # 3) сценарии от актуальной цены, включая неблагоприятный
    sc = d.get("сценарии") if isinstance(d.get("сценарии"), dict) else {}
    px, pdate = sc.get("цена"), _date(sc.get("дата_цены"))
    if not _num(px) or not pdate or not str(sc.get("источник_цены") or "").strip():
        g.append("сценарии: нет текущей цены с датой и источником")
    elif (today - pdate).days > STALE_DAYS:
        g.append(f"сценарии: цена от {pdate} старше {STALE_DAYS} дн. — пересчитать от актуальной")
    for key in ("базовый", "неблагоприятный"):
        v = sc.get(key) if isinstance(sc.get(key), dict) else {}
        if not _num(v.get("цена")) or len(str(v.get("основание") or "")) < 20:
            g.append(f"сценарии: «{key}» — цена и основание расчёта")
    adv = (sc.get("неблагоприятный") or {}).get("цена") if isinstance(sc.get("неблагоприятный"), dict) else None
    if _num(px) and _num(adv):
        long_ = str(d.get("направление") or "long") != "short"
        if (long_ and adv >= px) or (not long_ and adv <= px):
            g.append("сценарии: неблагоприятный сценарий не показывает потерь от текущей цены")
    if CEILING_RE.search(json.dumps(sc, ensure_ascii=False)):
        g.append("исторический максимум движения выдан за потолок — это не физический предел")
    # 4) горизонт, условия реализации и опровержения
    if not (_date(d.get("горизонт")) or _num(d.get("горизонт_недель"))):
        g.append("горизонт: дата или число недель")
    if not d.get("условия_реализации"):
        g.append("условия реализации")
    kills = d.get("условия_опровержения") or []
    if not kills or not any(re.search(r"\d", str(k)) for k in kills):
        g.append("условия опровержения: хотя бы одно измеримое (с числом или датой)")
    # 5) независимая критика и ответы на существенные возражения
    cr = d.get("критика") if isinstance(d.get("критика"), dict) else {}
    objs = [o for o in (cr.get("возражения") or []) if isinstance(o, dict)]
    if not str(cr.get("семейство") or "").strip() or not objs:
        g.append("независимая критика (модель другого семейства) с возражениями")
    unanswered = [o for o in objs if o.get("существенное") and len(str(o.get("ответ") or "")) < 20]
    if unanswered:
        g.append(f"нет ответа на существенные возражения: {len(unanswered)}")
    # 6) вероятности — не придумывать
    if _num(d.get("вероятность")) and not re.search(r"\d+\s*(?:из|/)\s*\d+", str(d.get("основание_вероятности") or "")):
        g.append("вероятность без счёта (k из n похожих случаев) — придуманный процент; пиши «неизвестна»")
    return g


# ── продвижение гипотезы ────────────────────────────────────────────────────────────
def advance(s, hid, outcome, data=None, reject_kind=None, reason="", needs=None, today=None):
    """Итог текущего этапа гипотезы: outcome 'pass' | 'reject' | 'needs_data'.

    pass на «быстрой проверке» требует quick_check_gaps = [], на «оценке» — worthy_gaps = []; иначе отказ
    продвинуть с перечнем недостающего (критерии не ослабляются ради ответа). pass на «результате» →
    поиск FOUND. reject → направление по виду отказа. → (гипотеза, сообщение)."""
    if s["статус"] != RUNNING:
        return None, f"поиск {STATUS_RU[s['статус']]} — сначала продолжить (resume)"
    h = _hyp(s, hid)
    if not h:
        return None, f"нет гипотезы {hid}"
    if h["вердикт"] in (REJECTED, WORTHY):
        return h, f"гипотеза {hid} уже закрыта ({h['вердикт']})"
    if data:
        h["данные"].update({k: v for k, v in data.items() if v is not None})
    stage = h["этап"]
    if outcome == "reject":
        if reject_kind not in REJECT_KINDS:
            return h, f"вид отказа — один из: {', '.join(REJECT_KINDS)}"
        if len(str(reason or "")) < 20:
            return h, "причина отказа ≥20 знаков: что именно опровергло"
        h["вердикт"] = REJECTED
        h["отказ"] = {"вид": reject_kind, "причина": str(reason)[:500], "этап": stage, "когда": _now()}
        h["история"].append({"этап": stage, "итог": "отказ", "вид": reject_kind})
        if reject_kind == "no_data":         # «не хватает данных» = не отказ по существу, а задача добыть
            h["вердикт"], h["отказ"] = NEEDS_DATA, None
            h["нужно_данных"] = list(needs or [reason])
        dr = None
        if reject_kind != "no_data":
            dr = _add_direction(s, reject_kind, h, f"{REJECT_KINDS[reject_kind]} (после {h['тикер']}: {str(reason)[:160]})")
        _log(s, "отказ", id=hid, вид=reject_kind, этап=stage)
        if reject_kind == "no_data":
            return h, f"гипотеза {hid}: не хватает данных — добыть; остальные направления не ждут"
        return h, (f"гипотеза {hid} отклонена ({reject_kind}); поиск продолжается" +
                   (f": новое направление {dr['id']} — {dr['текст'][:160]}" if dr else ""))
    if outcome == "needs_data":
        h["вердикт"] = NEEDS_DATA
        h["нужно_данных"] = list(needs or [reason or "данные не названы"])
        h["история"].append({"этап": stage, "итог": "нужны данные"})
        _log(s, "нужны_данные", id=hid, этап=stage)
        return h, f"гипотеза {hid}: нужны данные ({'; '.join(h['нужно_данных'])[:200]}); остальные направления не ждут"
    if outcome != "pass":
        return h, "outcome — pass | reject | needs_data"
    if stage == "быстрая_проверка":
        gaps = quick_check_gaps(h["данные"])
        if gaps:
            return h, "быстрая проверка не пройдена — не хватает: " + "; ".join(gaps)
    if stage == "оценка":
        gaps = worthy_gaps(h, today)
        if gaps:
            return h, "кандидат пока не достоин выдачи — не хватает: " + "; ".join(gaps)
    h["вердикт"] = OPEN if h["вердикт"] == NEEDS_DATA else h["вердикт"]
    h["нужно_данных"] = []
    h["история"].append({"этап": stage, "итог": "пройден"})
    if stage == "результат":
        gaps = worthy_gaps(h, today)       # актуальность ещё раз прямо перед выдачей
        if gaps:
            return h, "перед выдачей кандидат устарел — не хватает: " + "; ".join(gaps)
        h["вердикт"] = WORTHY
        s["статус"], s["итог"] = FOUND, {"гипотеза": hid, "тикер": h["тикер"], "когда": _now()}
        _log(s, "найден", id=hid, тикер=h["тикер"])
        return h, f"найден достойный кандидат: {h['тикер']} ({hid}) — поиск завершён"
    h["этап"] = STAGES[STAGES.index(stage) + 1]
    _log(s, "этап", id=hid, этап=h["этап"])
    return h, f"гипотеза {hid}: этап «{stage}» пройден → «{h['этап']}»"


# ── что делать дальше ───────────────────────────────────────────────────────────────
STAGE_TODO = {
    "генерация": "сформулировать проверяемо: событие → механизм → получатель прибыли → бумага; почему рынок не видит",
    "быстрая_проверка": "до длинного разбора: ключевые факты с источником, причинный механизм, текущая цена, оставшийся "
                        "потенциал, существенные риски, доступность инструмента; слабое — отсечь сразу",
    "углубление": "что рынок уже ждёт (implied_expectations / консенсус EODHD) и наше отличие с цифрами, существенность "
                  "(materiality_check), сценарии от актуальной цены (базовый и неблагоприятный), горизонт, условия "
                  "реализации и опровержения",
    "критика": "независимая критика моделью другого семейства (court_check) и ответ на каждое существенное возражение",
    "оценка": "собрать всё и проверить кодом: факты с датами публикации и проверки, market_view с основанием, сценарии",
    "результат": "перепроверить свежую цену и катализаторы прямо перед выдачей и выдать кандидата",
}


def queue_summary(s):
    hs = s["гипотезы"]
    return {"гипотез": len(hs), "открыто": sum(h["вердикт"] == OPEN for h in hs),
            "нужны_данные": sum(h["вердикт"] == NEEDS_DATA for h in hs),
            "отклонено": sum(h["вердикт"] == REJECTED for h in hs),
            "направлений_не_взято": sum(not dr["взято"] for dr in s["направления"])}


def next_action(s):
    """Следующий шаг поиска словами (для агента/охотника). Ожидание по одному кандидату не блокирует остальных."""
    st = s["статус"]
    if st == FOUND:
        return {"шаг": "готово", "текст": f"найден кандидат {s['итог']['тикер']}"}
    if st == CANCELLED:
        return {"шаг": "отменён", "текст": "поиск отменён владельцем"}
    if st == PAUSED:
        return {"шаг": "пауза", "текст": (s.get("пауза") or {}).get("причина") or "пауза"}
    opened = [h for h in s["гипотезы"] if h["вердикт"] == OPEN]
    if opened:
        h = min(opened, key=lambda x: STAGES.index(x["этап"]))   # сперва дешёвые этапы — быстро отсекать
        return {"шаг": "этап", "гипотеза": h["id"], "этап": h["этап"],
                "текст": f"{h['id']} {h['тикер']} — этап «{h['этап']}»: {STAGE_TODO[h['этап']]}"}
    dirs = [dr for dr in s["направления"] if not dr["взято"]]
    if dirs:
        dr = dirs[0]
        return {"шаг": "направление", "направление": dr["id"],
                "текст": f"новые гипотезы по направлению {dr['id']}: {dr['текст']}"}
    data = [h for h in s["гипотезы"] if h["вердикт"] == NEEDS_DATA]
    if data:
        h = data[0]
        return {"шаг": "данные", "гипотеза": h["id"],
                "текст": f"добыть данные для {h['id']} {h['тикер']}: {'; '.join(h['нужно_данных'])[:300]}"}
    closed = [f"{h['тикер']} ({h['отказ']['вид']})" for h in s["гипотезы"] if h["вердикт"] == REJECTED]
    return {"шаг": "генерация", "текст": ("сгенерировать новые гипотезы по теме «" + s["тема"] + "» — другие события, "
                                          "получатели прибыли, способы выражения" +
                                          (f"; уже отвергнуто, не повторять без новых данных: {', '.join(closed)}"
                                           if closed else ""))}


def status_line(s):
    """Строка статуса для владельца — пишет КОД (не модель): что с поиском, почему, что дальше."""
    q = queue_summary(s)
    head = f"Поиск «{s['тема'][:80]}»: {STATUS_RU[s['статус']]}"
    if s["статус"] == FOUND:
        return head + f" — {s['итог']['тикер']}."
    tail = (f" В очереди: гипотез {q['гипотез']} (открыто {q['открыто']}, ждут данных {q['нужны_данные']}, "
            f"отклонено {q['отклонено']}), направлений не взято {q['направлений_не_взято']}.")
    if s["статус"] == PAUSED:
        p = s.get("пауза") or {}
        how = {"turn": "Сам в фоне не продолжаю — фонового исполнителя нет. Напиши «продолжай», и я продолжу с этого места.",
               "budget": "Бюджет поиска сам не повышаю — реши, поднимать ли.",
               "month": "Месячный лимит расходов исчерпан — до решения владельца поиск стоит.",
               "queue_empty": "Направления по теме исчерпаны — нужна новая тема или новый факт.",
               "executor": "Исполнитель недоступен — продолжу, когда он снова будет запущен."}.get(p.get("вид"), "")
        return head + f": {p.get('причина')}." + tail + " " + how
    return head + "." + tail


def summary(s):
    return {"id": s["id"], "тема": s["тема"], "статус": s["статус"], "статус_ru": STATUS_RU[s["статус"]],
            "пауза": s.get("пауза"), "итог": s.get("итог"), "бюджет": s["бюджет"], "очередь": queue_summary(s),
            "следующий_шаг": next_action(s),
            "гипотезы": [{k: h.get(k) for k in ("id", "тикер", "этап", "вердикт", "отказ", "нужно_данных", "круг",
                                                "получатель")} for h in s["гипотезы"]],
            "направления": s["направления"]}


# ── облачный охотник: итог запуска → состояние поиска ─────────────────────────────────
def from_hunt(none_path=None, card_path=None, topic="", date=None, d=None, gate=None):
    """Итог запуска hunt-idea.js → поиск исполнителя 'hunter'. Честный ноль — НЕ успех: проверенные ветки
    становятся закрытыми гипотезами (с видом отказа) или ждущими данных, поиск — на паузе с причиной
    «запуск охоты закончился», очередь сохраняется для следующего запроса. Карточка, прошедшая шлагбаум
    кодом (idea_gate + evidence_check), — найденный кандидат."""
    date = date or _today()
    cur = active("hunter", d)
    s = cur if cur and cur["тема"] == str(topic or "")[:300] else start(topic, "hunter", d=d)
    if s["статус"] == PAUSED:
        resume(s, by="новый запуск охоты")
    if card_path:
        card = json.loads(pathlib.Path(card_path).read_text(encoding="utf-8"))
        if gate is None:
            from ops import idea_gate as IG
            res = IG.check(card, today=_date(date))
            gate_ok = bool(res.get("pass"))
        else:
            gate_ok = bool(gate)
        h, err = add_hypothesis(s, card.get("thesis") or card.get("name") or "карточка охотника",
                                card.get("ticker"), card.get("mechanism") or card.get("mechanism_id") or card.get("thesis"),
                                card.get("name") or "")
        h = h or next((x for x in s["гипотезы"] if _similar(fingerprint(card.get("ticker"), card.get("mechanism")
                                                                        or card.get("mechanism_id") or card.get("thesis")),
                                                            x["отпечаток"])), None)
        if h and gate_ok:
            h["этап"], h["вердикт"] = "результат", WORTHY
            h["основание"] = f"шлагбаум кодом охотника пройден: {pathlib.Path(card_path).name}"
            s["статус"], s["итог"] = FOUND, {"гипотеза": h["id"], "тикер": h["тикер"], "когда": _now(),
                                             "карточка": str(card_path)}
            _log(s, "найден", id=h["id"], тикер=h["тикер"], основание="idea_gate")
        elif h:
            h["вердикт"], h["нужно_данных"] = NEEDS_DATA, ["карточка не прошла шлагбаум кодом — довести доказательства"]
    if none_path:
        z = json.loads(pathlib.Path(none_path).read_text(encoding="utf-8"))
        for br in z.get("branches") or []:
            what = str(br.get("what") or "")
            m = re.search(r"\b([A-Z]{1,5}(?:\.[A-Z]{1,3})?)\b", what)
            tk = m.group(1) if m else what[:12]
            h, _err = add_hypothesis(s, what if len(what) >= 20 else what + " — ветка охоты", tk,
                                     what if len(what) >= 10 else what + " (механизм ветки)")
            if not h:
                continue
            kind = br.get("kind") if br.get("kind") in REJECT_KINDS else "no_data"
            advance(s, h["id"], "reject", reject_kind=kind,
                    reason=(str(br.get("why_not") or "") + " ").ljust(20, "·")[:500],
                    needs=[str(br.get("return_if") or "факт, который вернёт к ветке")])
            if br.get("return_if") and h["отказ"]:
                h["отказ"]["вернуться_если"] = str(br["return_if"])[:300]
        bc = z.get("best_candidate") or {}
        if bc.get("ticker"):
            h, _err = add_hypothesis(s, f"{bc.get('name') or bc['ticker']}: лучший кандидат запуска", bc["ticker"],
                                     str(bc.get("what_missing") or "лучший кандидат без доказательной базы"))
            if h:
                advance(s, h["id"], "needs_data", needs=[str(bc.get("what_missing") or ""),
                                                         "станет идеей, если: " + str(bc.get("becomes_idea_if") or "")])
    if s["статус"] == RUNNING:
        q = queue_summary(s)
        pause(s, "executor", f"запуск облачной охоты {date} закончился без достойного кандидата (проверено "
                             f"веток {q['отклонено'] + q['нужны_данные']}); продолжение — следующий запуск по запросу")
    save(s, d)
    return s


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Очередь поиска идеи (VQ-4)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sp = sub.add_parser("show")
    sp.add_argument("sid")
    fh = sub.add_parser("from-hunt", help="итог запуска охоты → состояние поиска (честный ноль = пауза, не успех)")
    fh.add_argument("--none", dest="none_path")
    fh.add_argument("--card")
    fh.add_argument("--topic", default="")
    fh.add_argument("--date")
    a = ap.parse_args(argv)
    if a.cmd == "from-hunt":
        s = from_hunt(a.none_path, a.card, a.topic, a.date)
        print(status_line(s))
        return 0
    if a.cmd == "list":
        for s in all_searches():
            print(s["id"], STATUS_RU[s["статус"]], "—", s["тема"][:60])
    else:
        print(json.dumps(summary(load(a.sid)), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
