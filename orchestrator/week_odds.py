# -*- coding: utf-8 -*-
"""orchestrator/week_odds.py — V5 «Шансы недели»: тезисы владельца + библиотека механизмов →
проверяемые §9-вопросы о событиях 3–13 недель, с вероятностями экономиста потоков.

Конструкция (карта v3, подписи владельца 31.10/01.11):
  • ГЕНЕРАЦИЯ: LLM-роль flow_economist (слияние фундаментальной школы и теории игр,
    промпт agents/prompts/v5_flow_economist.md) разворачивает knowledge/theses_owner.yaml
    и knowledge/mechanisms.yaml в кандидатов-вопросы. LLM НЕ решает, что печатать.
  • ВАЛИДАЦИЯ: детерминированный код (здесь) — §9-разрешимость (актив/порог/дата/источник
    цены), горизонт строго 3–13 недель, вероятность в [0.05..0.95], формат тикера. Брак
    отбрасывается с причиной (П8: отказ лучше мусора).
  • ПЕЧАТЬ: mathlib.sealing.seal в ОТДЕЛЬНЫЙ герметичный трек kind=thesis_event
    (к §11-деньгам не приближается; счёт форварда копится НА тезисе/механизме —
    владелец увидит «этот взгляд пока сбывался в X%»).
  • Протокол прогона: journal/week_odds/wo_<ts>.json (не запечатанный журнал — сводка).

Бюджет: один LLM-вызов за прогон, пишется в costs.jsonl (LiveClient). Mock-клиент этого
output_kind не знает → честный ОТКАЗ без печатей (прогоны — только live или со стабом в тестах).
Боевой крон — ПОСЛЕ ручного прогона испытателя и «ок» владельца (правило внедрения 01.11).
"""
import json
import pathlib
import datetime
import argparse
import re
import sqlite3
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import sealing as SEAL                    # noqa: E402
from mathlib import priced_in as PIN                   # noqa: E402
from orchestrator.openrouter import make_client        # noqa: E402

THESES = ROOT / "knowledge" / "theses_owner.yaml"
MECHS = ROOT / "knowledge" / "mechanisms.yaml"
PROMPT = ROOT / "agents" / "prompts" / "v5_flow_economist.md"
OUT_DIR = ROOT / "journal" / "week_odds"

KIND = "thesis_event"
TRACK = "thesis_event"
ROLE = "flow_economist"
MIN_WEEKS, MAX_WEEKS = 3, 13
P_MIN, P_MAX = 0.05, 0.95
MAX_QUESTIONS = 12          # аудит 03.11: при 8 вопросах раз в неделю к 01.02 не набрать 60 печатей/40 исходов
MAX_PER_MECH = 2            # не больше 2 вопросов на механизм за прогон (иначе трек = один взгляд)
SHORT_WEEKS = 6             # «короткие» вопросы (3–6 нед) — успевают сверяться до точки Б
SHORT_SHARE = 0.5           # доля коротких, которую просим у экономиста (факт — в протоколе)
MIN_SIGMA_MOVE = 0.5        # |ход| < 0.5σ√T — вопрос о шуме, а не о взгляде
MECH_WINDOW = 14            # механизмов в промпте за прогон (ротация по всей библиотеке)
TICKER_RE = re.compile(r"^[A-Z0-9]{1,8}\.US$")
# идентичность ставки трека: один и тот же вопрос не печатаем дважды
DEDUP_FIELDS = ("track", "asset", "direction", "threshold", "resolve_by")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def load_theses(path=None):
    p = pathlib.Path(path or THESES)
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("тезисы") or {}


def mechanism_ids(path=None):
    """id всех торгуемых механизмов библиотеки (сверка mechanism_id экономиста)."""
    p = pathlib.Path(path or MECHS)
    if not p.exists():
        return set()
    rows = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("механизмы") or []
    return {str(m.get("id")) for m in rows if m.get("tradeable") and m.get("id")}


def load_mechanisms(path=None, top=MECH_WINDOW, rotation=None):
    """Торгуемые механизмы с прецедентами, компактно для промпта (не весь YAML на 280КБ).

    Аудит 03.11: раньше всегда шли одни и те же 14 «самых документированных» из 48 — трек мерил
    треть библиотеки. rotation=k (номер прогона) → окно из `top` механизмов по кругу, по id."""
    p = pathlib.Path(path or MECHS)
    if not p.exists():
        return []
    rows = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("механизмы") or []
    rows = [m for m in rows if m.get("tradeable") and (m.get("precedents") or [])]
    if rotation is None:
        rows.sort(key=lambda m: -len(m.get("precedents") or []))
        window = rows[:top]
    else:
        rows.sort(key=lambda m: str(m.get("id")))
        start = (int(rotation) * top) % len(rows) if rows else 0
        window = (rows[start:] + rows[:start])[:top]
    out = []
    for m in window:
        out.append({"mechanism_id": m.get("id"), "rule": m.get("rule"),
                    "trigger": m.get("trigger_class"), "lag_weeks": m.get("lag_weeks"),
                    "watch": m.get("watch_assets"),
                    "precedents": [f"{pr.get('year')}: {pr.get('observed')}"
                                   for pr in (m.get("precedents") or [])[:2]]})
    return out


_TICK_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5})(?![A-Za-z0-9])")
_NOT_TICKERS = {"TTF", "LNG", "CFR", "FOB", "VLCC", "TCE", "BDTI", "WCI", "SCFI", "FBX", "ETF", "ESPO",
                "WTI", "USA", "OPEC", "GDP", "CPI", "AI", "PPA", "EU", "US", "IEA", "LME", "EUA", "FX"}


def watch_tickers(mechs, theses=None, limit=40):
    """Тикеры-кандидаты для таблицы цен в промпте: из watch механизмов и цепочек тезисов
    (свободный текст → токены ВЕРХНЕГО регистра 2–5 букв, без явных не-тикеров)."""
    seen = []
    texts = [str(m.get("watch") or "") for m in (mechs or [])]
    for t in (theses or {}).values():
        texts += [json.dumps(t.get("кандидаты_цепочек") or "", ensure_ascii=False)]
    for txt in texts:
        for tok in _TICK_RE.findall(txt):
            if tok not in _NOT_TICKERS and tok not in seen:
                seen.append(tok)
    return seen[:limit]


def build_user_prompt(theses, mechs, now=None, prices=None):
    now = now or _now()
    lines = [f"Сегодня {now.date().isoformat()}. Горизонт вопросов: {MIN_WEEKS}–{MAX_WEEKS} недель.",
             "", "ТЕЗИСЫ ВЛАДЕЛЬЦА:"]
    if prices:
        # 03.11 (ручной прогон для «ок» владельца): экономист ставил пороги по памяти — USO $85 при
        # $156.87, SMH $250 при $508 — 6 из 8 вопросов в брак. Теперь видит СЕРВЕРНЫЕ цены и задаёт
        # ход в процентах; порог в долларах считает код от той же цены (Инв#6).
        lines = lines[:2] + ["СВЕЖИЕ ЦЕНЫ ЗАКРЫТИЯ (сервер, EODHD) — опирайся на них, не на память:"] + \
            [f"- {a}: {px:.2f}" for a, px in prices.items()] + ["", "ТЕЗИСЫ ВЛАДЕЛЬЦА:"]
    for tid, t in theses.items():
        lines.append(f"- {tid}: {str(t.get('формулировка') or '').strip()}")
        for c in (t.get("наблюдаемые_следствия") or [])[:3]:
            lines.append(f"    следствие: {c}")
    lines += ["", "МЕХАНИЗМЫ БИБЛИОТЕКИ (выдержка):",
              json.dumps(mechs, ensure_ascii=False, indent=1),
              "", f"Верни до {MAX_QUESTIONS} вопросов JSON-массивом по формату из системного промпта.",
              f"Квоты (проверяет код): не меньше половины вопросов сроком {MIN_WEEKS}–{SHORT_WEEKS} недель; "
              f"не больше {MAX_PER_MECH} вопросов на один механизм; по одной бумаге — один вопрос; где механизм "
              "допускает падение, ставь и вопросы «below»; thesis_id и mechanism_id — ТОЛЬКО из перечисленных "
              "выше (выдуманный id — брак); ход в процентах должен быть заметно больше обычных колебаний бумаги."]
    return "\n".join(lines)


def parse_candidates(text):
    """Достаём первый JSON-массив из ответа модели. Непарсибельно → [] (честный ноль)."""
    if not isinstance(text, str):
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    return arr if isinstance(arr, list) else []


def validate_candidate(c, now=None, *, sealable_fn=None, last_close_fn=None):
    """Детерминированная §9-проверка кандидата. Возвращает (spec|None, причина_брака|None)."""
    now = now or _now()
    if not isinstance(c, dict):
        return None, "не dict"
    asset = str(c.get("актив") or "").strip().upper()
    if not TICKER_RE.match(asset):
        return None, f"актив не EODHD-тикер *.US: {asset!r}"
    direction = str(c.get("направление") or "").strip().lower()
    if direction not in ("above", "below"):
        return None, f"направление не above/below: {direction!r}"
    px_code = None
    if c.get("ход_проц") is not None and last_close_fn is not None:
        # 03.11: порог считает КОД — от серверной цены и хода экономиста в процентах
        try:
            move = float(c.get("ход_проц"))
        except (TypeError, ValueError):
            return None, "ход_проц не число"
        if abs(move) < 1.0:
            return None, f"ход {move}% меньше 1% — вопрос о шуме, а не о взгляде"
        if (move > 0) != (direction == "above"):
            return None, f"знак хода {move}% не совпадает с направлением {direction}"
        px_code = last_close_fn(asset)
        if px_code is None or px_code <= 0:
            return None, f"нет котировки для расчёта порога {asset} (П8) — не печатаем"
        threshold = round(px_code * (1 + move / 100.0), 2)
    else:
        try:
            threshold = float(c.get("порог"))
        except (TypeError, ValueError):
            return None, "порог не число"
    if threshold <= 0:
        return None, "порог должен быть > 0"
    try:
        weeks = int(c.get("срок_недель"))
    except (TypeError, ValueError):
        return None, "срок_недель не число"
    if not (MIN_WEEKS <= weeks <= MAX_WEEKS):
        return None, f"срок {weeks} нед вне зоны {MIN_WEEKS}–{MAX_WEEKS} (короче — отыграно, длиннее — не наше окно)"
    try:
        prob = float(c.get("вероятность"))
    except (TypeError, ValueError):
        return None, "вероятность не число"
    if not (P_MIN <= prob <= P_MAX):
        return None, f"вероятность {prob} вне [{P_MIN}..{P_MAX}]"
    вопрос = str(c.get("вопрос") or "").strip()
    почему = str(c.get("почему") or "").strip()
    if not вопрос or not почему:
        return None, "нет текста вопроса или «почему»"
    resolve_by = (now + datetime.timedelta(weeks=weeks)).replace(
        hour=20, minute=0, second=0, microsecond=0)
    if px_code is not None:
        # текст вопроса — из КОДА (в тексте модели остались бы её цены по памяти)
        # испытатель 10.11: модель пишет и «закроются» (фонды во множественном числе) — резать по обеим
        # формам, иначе «… закроются ниже … закроется ниже $X»
        label = re.split(r"\s+закро(?:ется|ются)\s+", вопрос, maxsplit=1)[0].strip().rstrip("….") \
            or asset.split(".")[0]
        months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                  "сентября", "октября", "ноября", "декабря")
        вопрос = (f"{label} закроется {'выше' if direction == 'above' else 'ниже'} ${threshold:.2f} "
                  f"к {resolve_by.day} {months[resolve_by.month - 1]}")
    spec = {
        "kind": KIND, "track": TRACK,
        "asset": asset, "direction": direction, "threshold": threshold,
        "resolve_by": resolve_by.isoformat(timespec="seconds"),
        "price_source": f"EODHD close {asset}",
        "probability": prob,                      # сырая P экономиста; калибровка трека копится форвардом
        "вероятность_источник": "flow_economist (не калибровано — трек копит свой счёт)",
        "horizon_weeks": weeks,
        "thesis_id": c.get("thesis_id") or None,
        "mechanism_id": c.get("mechanism_id") or None,
        "вопрос": вопрос, "почему": почему,
        "spec_ref": "карта v3 §V5 «Шансы недели»; §9 разрешимость; П16 форвард-онли",
    }
    problems = SEAL.validate_resolvable(spec)
    if problems:
        return None, "; ".join(problems)
    # Ре-гейт V0 02.11 (БЛОКЕР §9/П16): форма тикера ≠ источник цены. Галлюцинированный
    # LLM-тикер правильной формы печатался бы навечно pending — трек и счёт взглядов молча
    # голодали бы. sealable_fn (universe_resolver.is_sealable на сервере) обязателен в live.
    if sealable_fn is not None and not sealable_fn(asset):
        return None, f"нет источника цены/истории для {asset} (§9) — не печатаем"
    # Ре-гейт V0 02.11 (БЛОКЕР П8/§10): порог экономиста обязан быть заякорен к текущей
    # цене — иначе трек меряет ошибку ценового якоря LLM, а не взгляд. Без котировки —
    # честный брак (вопрос не печатается), вырожденный порог (вне 0.5×..2× цены) — брак.
    if last_close_fn is not None:
        px = last_close_fn(asset)
        if px is None or px <= 0:
            return None, f"нет котировки для якоря порога {asset} (П8) — не печатаем"
        if not (0.5 * px <= threshold <= 2.0 * px):
            return None, (f"порог {threshold} не заякорен к цене (~{px:.2f}): "
                          f"вне полосы 0.5×..2× — вырожденный вопрос (§10)")
        # Перепроверка №2 02.11: порог на УЖЕ-истинной стороне цены (above с порогом ≤ цены,
        # below с порогом ≥ цены) — почти гарантированный YES за горизонт: трек мерил бы
        # тривиальность порога, а не взгляд, и дул бы сбываемость «взгляда» владельцу.
        # Вопрос «удержится» формулируется зеркально — движением к дальней стороне.
        if (direction == "above" and threshold <= px) or \
                (direction == "below" and threshold >= px):
            return None, (f"порог {threshold} на уже-истинной стороне цены (~{px:.2f}) для "
                          f"{direction} — тривиальный вопрос, трек мерил бы не взгляд (§10)")
    return spec, None


def _amplitudes(mechs_path=None):
    """id механизма → текст прецедентной амплитуды (для «что уже в цене»)."""
    p = pathlib.Path(mechs_path or MECHS)
    if not p.exists():
        return {}
    rows = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("механизмы") or []
    return {m.get("id"): m.get("amplitude") for m in rows if m.get("id")}


def make_move_fn(db_path=None, now=None):
    """Серверный источник реализованного хода для «что уже в цене»: % изменения
    adjusted_close за окно недель по таблице quotes (storage/oracle.db, кормится
    EODHD-кроном 06:50). Честные None: нет БД/символа/истории, дыра в начале окна
    (> 2 нед), последняя котировка старше 2 недель. Детерминированно, сети нет."""
    db = pathlib.Path(db_path or ROOT / "storage" / "oracle.db")
    now0 = now or _now()

    def move(asset, weeks=6):
        if not asset or not db.exists():
            return None
        today = now0.date()
        since = today - datetime.timedelta(weeks=max(1, int(weeks or 6)))
        try:
            con = sqlite3.connect(str(db), timeout=10)
            rows = con.execute(
                "SELECT date, COALESCE(adjusted_close, close) FROM quotes "
                "WHERE symbol=? AND date>=? AND date<=? AND COALESCE(adjusted_close, close) "
                "IS NOT NULL ORDER BY date",
                (asset, (since - datetime.timedelta(weeks=2)).isoformat(),
                 today.isoformat())).fetchall()
            con.close()
        except sqlite3.Error:
            return None
        rows = [(d, p) for d, p in rows if p]
        if len(rows) < 2:
            return None
        last_d, last_p = rows[-1]
        if last_d < (today - datetime.timedelta(weeks=2)).isoformat():
            return None                                  # котировки протухли
        base = next(((d, p) for d, p in rows if d >= since.isoformat()), rows[0])
        if base[0] > (since + datetime.timedelta(weeks=2)).isoformat():
            return None                                  # дыра в начале окна
        if base[1] == 0 or base == (last_d, last_p):
            return None
        return (last_p / base[1] - 1.0) * 100.0

    return move


def annotate_priced_in(spec, amplitudes, move_fn):
    """Модуль «что уже в цене» (ПОДПИСАН 01.11): реализованный ход актива (в %, от move_fn)
    против прецедентной амплитуды механизма (mathlib/priced_in). Аннотация ставится ДО
    печати — контекст «сколько уже в цене» запечатывается вместе с вопросом (П16).
    Окно хода = горизонт вопроса (симметрично и объяснимо владельцу). Нет move_fn /
    нет амплитуды / ход не распознан → честно без поля, прогон не падает."""
    if move_fn is None:
        return spec
    amp = amplitudes.get(spec.get("mechanism_id"))
    if not amp:
        return spec
    try:
        try:
            mv = move_fn(spec.get("asset"), spec.get("horizon_weeks") or 6)
        except TypeError:                                # move_fn(asset) без окна
            mv = move_fn(spec.get("asset"))
    except Exception:                                    # noqa: BLE001 — источник цен упал
        mv = None
    if mv is None:
        return spec
    out = PIN.played_fraction(mv, amp)
    if "ОТКАЗ" not in out:
        spec["что_уже_в_цене"] = {"вердикт": out["вердикт"], "строка": out["строка"],
                                  "реализовано_пп": round(float(mv), 1)}
    return spec


def _closes_from_db(db_path=None):
    """closes_fn для базы шума: последние 61 закрытие (та же колонка, что у якоря) → (closes, last_date)."""
    db = pathlib.Path(db_path or ROOT / "storage" / "oracle.db")

    def closes(asset):
        if not asset or not db.exists():
            return None
        try:
            con = sqlite3.connect(str(db), timeout=10)
            rows = con.execute(
                "SELECT date, COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
                "AND COALESCE(adjusted_close, close) IS NOT NULL ORDER BY date DESC LIMIT 61",
                (asset,)).fetchall()
            con.close()
        except sqlite3.Error:
            return None
        if len(rows) < 21:
            return None
        return [float(r[1]) for r in reversed(rows)], str(rows[0][0])[:10]

    return closes


def noise_fields(spec, closes_fn):
    """База шума (mathlib/base_odds) и мера хода для печати: якорь с датой, ход %, ход в σ√T.
    → (поля|None, причина|None). Нет истории — честный брак: вопрос нельзя мерить против шума."""
    import math
    from mathlib import base_odds as BO
    got = closes_fn(spec["asset"]) if closes_fn else None
    if not got:
        return None, f"нет истории цен {spec['asset']} для базы шума — вопрос не измерим против шума"
    closes, last_date = got
    px = closes[-1]
    sig = BO.daily_sigma(closes)
    days = BO.trading_days(last_date, spec["resolve_by"][:10])
    if not sig or not days or px <= 0:
        return None, f"не посчитать колебания {spec['asset']} — вопрос не измерим против шума"
    z = math.log(spec["threshold"] / px) / (sig * math.sqrt(days))
    if abs(z) < MIN_SIGMA_MOVE:
        return None, (f"ход до порога {abs(z):.2f} обычных колебаний за срок (< {MIN_SIGMA_MOVE}) — "
                      "вопрос о шуме, а не о взгляде")
    base = BO.base_probability(px, spec["threshold"], sig, days, spec["direction"])
    return {"вероятность_база": round(base, 4) if base is not None else None,
            "якорь_цена": round(px, 4), "якорь_дата": last_date,
            "ход_проц": round((spec["threshold"] / px - 1) * 100, 2),
            "ход_в_сигмах": round(z, 3)}, None


def annotate_options(spec, options_fn):
    """Q-в-цене 09.11: что опционы закладывают к сроку вопроса (риск-нейтральная вероятность дойти до порога) —
    ставится ДО печати, рядом с нашей оценкой; потом видно, где мы расходимся с рынком. Сбой — честно без поля."""
    if options_fn is None:
        return spec
    try:
        res = options_fn(spec)
    except Exception as e:                                      # noqa: BLE001 — опционы не роняют выпуск
        res = {"ok": False, "причина": f"{type(e).__name__}"}
    if isinstance(res, dict) and res.get("ok") and res.get("вероятность_рн") is not None:
        spec["опционы"] = {"вероятность_рн": res["вероятность_рн"], "экспирация": res.get("экспирация"),
                           "ход_1сигма": (res.get("ход_1сигма") or {}).get("доля"),
                           "котировки_на": res.get("котировки_на"), "оговорки": res.get("оговорки") or [],
                           "источник": "EODHD опционы (Unicorn Bay) + mathlib/options_implied"}
    elif isinstance(res, dict):
        spec["опционы"] = {"нет": str(res.get("причина") or "не посчитано")[:160]}
    return spec


def make_options_fn():
    """Серверный источник: ops/options_view (ключ EODHD из .env), якорь — цена вопроса."""
    sys.path.insert(0, str(ROOT))
    from ops import options_view as OV

    def fn(spec):
        return OV.view(spec["asset"], spec["resolve_by"][:10], spec["threshold"], spec["direction"],
                       spot=spec.get("якорь_цена"), spot_date=spec.get("якорь_дата"))
    return fn


def _open_weeks(predictions_path=None):
    """(бумага, ISO-неделя сверки) уже запечатанных вопросов трека — одна бумага на неделю сверки."""
    p = pathlib.Path(predictions_path or ROOT / "journal" / ("predictions" + ".jsonl"))
    out = set()
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("kind") == KIND and r.get("asset") and r.get("resolve_by"):
            out.add((r["asset"], _iso_week(r["resolve_by"])))
    return out


def _iso_week(iso):
    try:
        y, w, _ = datetime.date.fromisoformat(str(iso)[:10]).isocalendar()
        return f"{y}-W{w:02d}"
    except ValueError:
        return "?"


def _last_close_from_db(db_path=None):
    """last_close_fn для якоря порога (§10): последний close из storage/oracle.db quotes,
    не старше 2 недель. Нет БД/символа/протухло → None (валидация честно бракует)."""
    db = pathlib.Path(db_path or ROOT / "storage" / "oracle.db")

    def last_close(asset):
        if not asset or not db.exists():
            return None
        try:
            con = sqlite3.connect(str(db), timeout=10)
            row = con.execute(
                "SELECT date, COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
                "AND COALESCE(adjusted_close, close) IS NOT NULL ORDER BY date DESC LIMIT 1",
                (asset,)).fetchone()
            con.close()
        except sqlite3.Error:
            return None
        if not row or not row[1]:
            return None
        stale = (datetime.datetime.now(datetime.timezone.utc).date()
                 - datetime.timedelta(weeks=2)).isoformat()
        return float(row[1]) if str(row[0]) >= stale else None

    return last_close


def run_week_odds(mode="auto", seal=False, now=None, theses_path=None, mechs_path=None,
                  predictions_path=None, out_dir=None, client=None, move_fn=None,
                  sealable_fn=None, last_close_fn=None, closes_fn=None, options_fn=None):
    """Полный прогон: тезисы+механизмы → экономист → валидация → [«что уже в цене»] →
    [печать] → протокол. move_fn(asset)→реализованный ход % — серверный источник цен;
    в облаке None → аннотации честно нет. sealable_fn/last_close_fn — гарды §9/§10
    (ре-гейт V0 02.11): CLI на сервере подставляет их сам."""
    now = now or _now()
    run_id = "wo_" + now.strftime("%Y%m%dT%H%M%SZ")
    theses = load_theses(theses_path)
    _od = pathlib.Path(out_dir or OUT_DIR)
    rotation = len(list(_od.glob("wo_*.json"))) if _od.exists() else 0      # номер прогона → окно
    mechs = load_mechanisms(mechs_path, rotation=rotation)
    known_mechs = mechanism_ids(mechs_path)
    итог = {"run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": mode,
            "печать_запрошена": bool(seal), "ротация": rotation,
            "механизмы_окна": [m.get("mechanism_id") for m in mechs],
            "тезисов": len(theses), "механизмов_подано": len(mechs),
            "кандидатов": 0, "принято": 0, "запечатано": 0, "брак": [], "вопросы": []}
    if not theses and not mechs:
        итог["ОТКАЗ"] = "нет ни тезисов, ни механизмов — генерировать не из чего (П8)"
        return _write_protocol(итог, out_dir)
    cl = client or make_client(mode=mode, run_id=run_id)
    # Ре-гейт №2 02.11: в протокол — ФАКТИЧЕСКИЙ режим клиента (auto без ключа = mock), а не
    # аргумент CLI; гард бота «mock-протокол не пушим» иначе мёртв для боевого `--mode auto`.
    итог["mode"] = getattr(cl, "mode", mode)
    # Ре-гейт V0 02.11 (БЛОКЕР Инв#5/§24): live-вызов экономиста — ТОЛЬКО через бюджетный
    # контур: пред-проверка месячного потолка + суб-потолок week_odds + стоп-на-лету.
    if getattr(cl, "mode", None) == "live":
        from orchestrator import run_budget as RB
        decision = RB.precheck("week_odds")
        if not decision.get("allowed"):
            итог["ОТКАЗ"] = f"бюджет §24: {decision.get('reason') or decision}"
            итог["ОТКАЗ_бюджет"] = decision
            return _write_protocol(итог, out_dir)
        cl.cost_guard = RB.RunBudgetGuard("week_odds", decision["cap_usd"])
    system = PROMPT.read_text(encoding="utf-8")
    if getattr(cl, "mode", None) == "live" and last_close_fn is None:
        last_close_fn = _last_close_from_db()
    prices = {}
    if last_close_fn is not None:
        for tk in watch_tickers(mechs, theses):
            try:
                px = last_close_fn(tk + ".US")
            except Exception:                            # noqa: BLE001 — таблица цен best-effort
                px = None
            if px:
                prices[tk + ".US"] = px
    итог["цен_в_запросе"] = len(prices)
    user = build_user_prompt(theses, mechs, now, prices=prices)
    from orchestrator import run_budget as RB
    try:
        resp = cl.complete(ROLE, system, user, agent_id="v5_flow_economist",
                           output_kind="week_odds_questions")
    except RB.RunBudgetExceeded as e:            # BaseException — отдельная ветка (прецедент challenge)
        итог["ОТКАЗ"] = (f"ОСТАНОВ бюджета §24 на лету: потрачено {e.spent_usd:.4f} "
                         f"при потолке {e.cap_usd} (режим {e.mode})")
        return _write_protocol(итог, out_dir)
    except Exception as e:                                   # noqa: BLE001 — честный отказ
        итог["ОТКАЗ"] = f"экономист недоступен: {type(e).__name__}: {e}"
        return _write_protocol(итог, out_dir)
    # live-гарды §9/§10 по умолчанию (не передали — подставляем серверные; mock их не трогает)
    if getattr(cl, "mode", None) == "live":
        if sealable_fn is None:
            from orchestrator import universe_resolver as U
            sealable_fn = U.is_sealable
        if last_close_fn is None:
            last_close_fn = _last_close_from_db()
        if closes_fn is None:
            closes_fn = _closes_from_db()
    cands = parse_candidates(resp.get("text") if isinstance(resp, dict) else resp)
    итог["кандидатов"] = len(cands)
    _amps = _amplitudes(mechs_path) if move_fn is not None else {}
    seen_local = set()
    per_mech, assets_local = {}, set()
    open_weeks = _open_weeks(predictions_path) if seal else set()
    for c in cands[:MAX_QUESTIONS * 2]:
        spec, why = validate_candidate(c, now, sealable_fn=sealable_fn,
                                       last_close_fn=last_close_fn)
        if spec is not None:
            # аудит 03.11: взгляд обязан быть из библиотеки — "null" и выдуманные id раньше проходили
            mid, tid = spec.get("mechanism_id"), spec.get("thesis_id")
            if mid in ("null", "None", ""):
                mid = spec["mechanism_id"] = None
            if tid in ("null", "None", ""):
                tid = spec["thesis_id"] = None
            if mid and mid not in known_mechs:
                spec, why = None, f"механизм «{mid}» не из библиотеки — выдуманный взгляд не печатаем"
            elif tid and tid not in theses:
                spec, why = None, f"тезис «{tid}» не из реестра владельца — не печатаем"
            elif not mid and not tid:
                spec, why = None, "вопрос без взгляда (ни тезиса, ни механизма) — нечего мерить"
            elif mid and per_mech.get(mid, 0) >= MAX_PER_MECH:
                spec, why = None, f"квота: уже {MAX_PER_MECH} вопроса по механизму «{mid}» в этом прогоне"
            elif spec["asset"] in assets_local:
                spec, why = None, "квота: по этой бумаге уже есть вопрос в прогоне"
            elif (spec["asset"], _iso_week(spec["resolve_by"])) in open_weeks:
                spec, why = None, "квота: по этой бумаге уже открыт вопрос с той же неделей сверки"
        if spec is not None and closes_fn is not None:
            nf, why = noise_fields(spec, closes_fn)
            if nf is None:
                spec = None
            else:
                spec.update(nf)
        if spec is None:
            итог["брак"].append({"кандидат": c if isinstance(c, dict) else str(c)[:120], "причина": why})
            continue
        key = (spec["asset"], spec["direction"], spec["threshold"], spec["resolve_by"])
        if key in seen_local:
            итог["брак"].append({"кандидат": spec["вопрос"], "причина": "дубль внутри прогона"})
            continue
        seen_local.add(key)
        if len(итог["вопросы"]) >= MAX_QUESTIONS:
            break
        spec["run_id"] = run_id
        if spec.get("mechanism_id"):
            per_mech[spec["mechanism_id"]] = per_mech.get(spec["mechanism_id"], 0) + 1
        assets_local.add(spec["asset"])
        annotate_priced_in(spec, _amps, move_fn)
        annotate_options(spec, options_fn)
        if seal:
            sealed = SEAL.seal(dict(spec), path=predictions_path, dedup_fields=DEDUP_FIELDS)
            if sealed is None:
                итог["брак"].append({"кандидат": spec["вопрос"], "причина": "дубль в журнале (уже печатали)"})
                continue
            spec["hash"] = sealed.get("hash")
            итог["запечатано"] += 1
        итог["принято"] += 1
        итог["вопросы"].append(spec)
    return _write_protocol(итог, out_dir)


def _quota_note(итог):
    qs = итог.get("вопросы") or []
    if qs:
        short = sum(1 for q in qs if int(q.get("horizon_weeks") or 99) <= SHORT_WEEKS)
        below = sum(1 for q in qs if q.get("direction") == "below")
        итог["квота_сроков"] = {"коротких": short, "всего": len(qs),
                                "выполнена": short >= SHORT_SHARE * len(qs), "на_снижение": below}
    return итог


def _write_protocol(итог, out_dir=None):
    _quota_note(итог)
    d = pathlib.Path(out_dir or OUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{итог['run_id']}.json").write_text(
        json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    return итог


def main(argv=None):
    ap = argparse.ArgumentParser(description="V5 «Шансы недели»: тезисы/механизмы → §9-вопросы")
    ap.add_argument("--mode", default="auto", choices=["auto", "live", "mock"])
    ap.add_argument("--seal", action="store_true", help="печатать принятые вопросы (иначе сухой прогон)")
    ap.add_argument("--show", action="store_true",
                    help="показать текст ровно так, как он уйдёт владельцу в Telegram (ручной прогон испытателя)")
    a = ap.parse_args(argv)
    try:
        _opt = make_options_fn()
    except Exception:                                           # noqa: BLE001 — без опционов выпуск живёт
        _opt = None
    r = run_week_odds(mode=a.mode, seal=a.seal, move_fn=make_move_fn(), options_fn=_opt)
    if a.show:
        # 03.11: ручной прогон перед «ок» владельца — показываем подачу, а не только счётчики
        sys.path.insert(0, str(ROOT / "ops"))
        import bot_reports as R
        text, viol = R.format_week_odds(r)
        print(text if text else "ПОДАЧА НЕ ПРОШЛА ЛИНТ: " + "; ".join(str(v) for v in viol))
        for b in r.get("брак", [])[:8]:
            print("  брак:", str(b)[:200])
    print(f"[week_odds] {r['run_id']}: кандидатов {r.get('кандидатов', 0)}, принято {r.get('принято', 0)}, "
          f"запечатано {r.get('запечатано', 0)}, брака {len(r.get('брак', []))}"
          + (f" · ОТКАЗ: {r['ОТКАЗ']}" if r.get("ОТКАЗ") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
