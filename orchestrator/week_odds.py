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
MAX_QUESTIONS = 8
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


def load_mechanisms(path=None, top=14):
    """Торгуемые механизмы с прецедентами, компактно для промпта (не весь YAML на 280КБ)."""
    p = pathlib.Path(path or MECHS)
    if not p.exists():
        return []
    rows = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("механизмы") or []
    rows = [m for m in rows if m.get("tradeable") and (m.get("precedents") or [])]
    rows.sort(key=lambda m: -len(m.get("precedents") or []))
    out = []
    for m in rows[:top]:
        out.append({"mechanism_id": m.get("id"), "rule": m.get("rule"),
                    "trigger": m.get("trigger_class"), "lag_weeks": m.get("lag_weeks"),
                    "watch": m.get("watch_assets"),
                    "precedents": [f"{pr.get('year')}: {pr.get('observed')}"
                                   for pr in (m.get("precedents") or [])[:2]]})
    return out


def build_user_prompt(theses, mechs, now=None):
    now = now or _now()
    lines = [f"Сегодня {now.date().isoformat()}. Горизонт вопросов: {MIN_WEEKS}–{MAX_WEEKS} недель.",
             "", "ТЕЗИСЫ ВЛАДЕЛЬЦА:"]
    for tid, t in theses.items():
        lines.append(f"- {tid}: {str(t.get('формулировка') or '').strip()}")
        for c in (t.get("наблюдаемые_следствия") or [])[:3]:
            lines.append(f"    следствие: {c}")
    lines += ["", "МЕХАНИЗМЫ БИБЛИОТЕКИ (выдержка):",
              json.dumps(mechs, ensure_ascii=False, indent=1),
              "", f"Верни до {MAX_QUESTIONS} вопросов JSON-массивом по формату из системного промпта."]
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
                  sealable_fn=None, last_close_fn=None):
    """Полный прогон: тезисы+механизмы → экономист → валидация → [«что уже в цене»] →
    [печать] → протокол. move_fn(asset)→реализованный ход % — серверный источник цен;
    в облаке None → аннотации честно нет. sealable_fn/last_close_fn — гарды §9/§10
    (ре-гейт V0 02.11): CLI на сервере подставляет их сам."""
    now = now or _now()
    run_id = "wo_" + now.strftime("%Y%m%dT%H%M%SZ")
    theses = load_theses(theses_path)
    mechs = load_mechanisms(mechs_path)
    итог = {"run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": mode,
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
    user = build_user_prompt(theses, mechs, now)
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
    cands = parse_candidates(resp.get("text") if isinstance(resp, dict) else resp)
    итог["кандидатов"] = len(cands)
    _amps = _amplitudes(mechs_path) if move_fn is not None else {}
    seen_local = set()
    for c in cands[:MAX_QUESTIONS * 2]:
        spec, why = validate_candidate(c, now, sealable_fn=sealable_fn,
                                       last_close_fn=last_close_fn)
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
        annotate_priced_in(spec, _amps, move_fn)
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


def _write_protocol(итог, out_dir=None):
    d = pathlib.Path(out_dir or OUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{итог['run_id']}.json").write_text(
        json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    return итог


def main(argv=None):
    ap = argparse.ArgumentParser(description="V5 «Шансы недели»: тезисы/механизмы → §9-вопросы")
    ap.add_argument("--mode", default="auto", choices=["auto", "live", "mock"])
    ap.add_argument("--seal", action="store_true", help="печатать принятые вопросы (иначе сухой прогон)")
    a = ap.parse_args(argv)
    r = run_week_odds(mode=a.mode, seal=a.seal, move_fn=make_move_fn())
    print(f"[week_odds] {r['run_id']}: кандидатов {r.get('кандидатов', 0)}, принято {r.get('принято', 0)}, "
          f"запечатано {r.get('запечатано', 0)}, брака {len(r.get('брак', []))}"
          + (f" · ОТКАЗ: {r['ОТКАЗ']}" if r.get("ОТКАЗ") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
