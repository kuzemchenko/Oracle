# -*- coding: utf-8 -*-
"""orchestrator/forecast.py — построение §9-разрешимых прогнозов и запечатывание (П16, §9).

Дирижёр (КОД, не LLM) формализует идею поля суждений в однозначный форвард-прогноз и
запечатывает его mathlib.seal ДО показа пользователю (инвариант 3 CLAUDE.md, скилл run-funnel п.6).

Почему порог (threshold) строит КОД, а не LLM (П16/П8):
  величина прогноза = ТЕКУЩИЙ close цены-сверки на дату запечатывания. Прогноз звучит как
  «<symbol> закроется ВЫШЕ/НИЖЕ своего сегодняшнего close $X к дате Y по цене EODHD close».
  Это полностью детерминированный, проверяемый ДО события уровень — никакой выдумки цены.
  Что МЕРЯЕТСЯ (и есть edge системы) — направление (above/below) и вероятность судьи; их
  система и предсказывает. raw close (не adjusted_close): adjusted задним числом меняется на
  дивидендах/сплитах — для запечатанного порога нужен НЕ переписываемый ряд (raw close).

resolve_by: дата запечатывания + горизонт идеи (торговые дни → календарные ×7/5), 20:00 UTC.
probability: вероятность судьи (этап 5) — код берёт её из кандидата, LLM её здесь не трогает.
"""
import datetime
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mathlib import sealing as SEAL          # noqa: E402
from orchestrator import synthesis as SY     # noqa: E402

# направление идеи → сторона порога §9
_DIR_LONG = ("лонг", "long", "buy", "покупка", "вверх", "рост")
_DIR_SHORT = ("шорт", "short", "sell", "продажа", "вниз", "падение")


def direction_to_side(direction):
    """'лонг'/'шорт' → 'above'/'below' (VALID_DIRECTIONS §9). None — не распознано.

    M12 (ревью 04.07): матч по ТОКЕНАМ, не подстрочный — «не лонг» раньше давал above и уходил
    в ЗАПЕЧАТАННЫЙ прогноз с перевёрнутой стороной. Отрицание рядом с токеном → None (честный
    отказ формализовать, идея не запечатывается — П8)."""
    d = str(direction or "").strip().lower()
    tokens = d.replace("-", " ").split()
    if any(neg in tokens for neg in ("не", "not", "без")):
        return None
    if any(w in tokens for w in _DIR_LONG):
        return "above"
    if any(w in tokens for w in _DIR_SHORT):
        return "below"
    return None


def _resolve_by(now_dt, horizon_trading_days):
    """Дата сверки: торговые дни → календарные (×7/5), отсечка 20:00 UTC (закрытие US-сессии)."""
    cal_days = max(1, math.ceil(horizon_trading_days * 7.0 / 5.0))
    d = (now_dt + datetime.timedelta(days=cal_days)).date()
    return datetime.datetime(d.year, d.month, d.day, 20, 0, 0,
                             tzinfo=datetime.timezone.utc).isoformat()


def build_forward_prediction(candidate, ctx, *, run_id, kind, now_dt=None,
                             horizon_days=None, probability=None):
    """Собрать §9-прогноз из идеи поля суждений. Возвращает (prediction|None, причина).

    prediction=None → идею НЕЛЬЗЯ запечатать честно (нет цены/направления/символа) — это
    законный результат (П8): лучше не запечатать, чем впустить неоднозначный прогноз.
    """
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    asset = candidate.get("актив")
    side = direction_to_side(candidate.get("направление"))
    if not asset:
        return None, "нет актива"
    if side is None:
        return None, f"направление не распознано: {candidate.get('направление')!r}"

    q = (ctx or {}).get("quotes", {}).get(asset)
    if not q or not q.get("last") or q["last"].get("close") is None:
        return None, f"нет цены close для {asset} (price_source) — не запечатываем (П8)"
    threshold = round(float(q["last"]["close"]), 4)
    last_date = q.get("last_date")

    prob = probability if probability is not None else candidate.get("вероятность_судьи")

    hdays = horizon_days if horizon_days is not None else (SY._idea_horizon_days(candidate)
                                                           or SY._DEFAULT_HORIZON_DAYS)
    pred = {
        "kind": kind,                                    # funnel_forward | theme_daily | calibration
        "run_id": run_id,
        "asset": asset,
        "direction": side,
        "threshold": threshold,
        "resolve_by": _resolve_by(now_dt, hdays),
        "price_source": f"EODHD close {asset}",
        "probability": (None if prob is None else round(float(prob), 4)),
        "base_rate": candidate.get("base_rate"),
        "school": candidate.get("школа"),
        "thesis": candidate.get("тезис"),
        "horizon_trading_days": round(float(hdays), 2),
        "threshold_asof_close_date": last_date,          # дата close, ставшего порогом (трассировка)
        "spec_ref": "§9 разрешимость; §17 режимы; П16 форвард-онли",
    }
    problems = SEAL.validate_resolvable(pred)
    if problems:
        return None, "§9: " + "; ".join(problems)
    return pred, "ok"


# Идентичность СТАВКИ для идемпотентности (ревью 2026-07-04): тот же актив, та же сторона,
# тот же порог, тот же срок = тот же прогноз. Перезапуск прогона в тот же день (ручной рестарт
# после сбоя, дубль cron) не должен плодить коррелированные дубли в журнале.
# B4 (05.07, stage-review): + track — дедуп внутри-ТРЕКОВЫЙ, как и сами счета (B3c/§R3):
#   • сырой kind в identity был БЫ регрессией — kind'ы одного money-трека (funnel_forward/
#     theme_daily/cascade_money) сливаются в один §11-Brier и счёт ворот-270, идентичная ставка
#     между ними — дубль, надувающий ворота (гейт stage-review B4);
#   • совсем без track'а edge_forward (lag 0 → тот же close/resolve_by) молча гасил бы легитимный
#     провизорный прогноз выдачи 09:00 — кросс-трековый дедуп ложен, у каждого трека свой счёт.
# Класс трека — resolve.track_for_kind (единый источник герметичности); поле пишется в запись.
DEDUP_FIELDS = ("track", "asset", "direction", "threshold", "resolve_by")


def dedup_normalize(rec):
    """Нормализация записи ДЛЯ СРАВНЕНИЯ дедупа (журнал не редактируется, П16): у легаси-записей
    (до 05.07) поля track нет — выводим его из kind, иначе повтор прогона в переходный день
    задваивал бы ставки, запечатанные до деплоя (повторный гейт B4, блокер)."""
    if rec.get("track") is not None:
        return rec
    from orchestrator import resolve as RES      # локально: не тянуть resolve при импорте forecast
    return {**rec, "track": RES.track_for_kind(rec.get("kind"))}


def seal_prediction(prediction, path=None):
    """Запечатать готовый §9-прогноз (mathlib.seal: append + hash). Проставляет класс трека
    (prediction['track']) для внутри-трекового дедупа. Возвращает запечатанную запись или None,
    если ИДЕНТИЧНАЯ ставка ТОГО ЖЕ ТРЕКА уже в журнале (дедуп)."""
    from orchestrator import resolve as RES      # локально: не тянуть resolve при импорте forecast
    prediction = dict(prediction)
    prediction["track"] = RES.track_for_kind(prediction.get("kind"))
    return SEAL.seal(prediction, path=path, dedup_fields=DEDUP_FIELDS,
                     dedup_normalize=dedup_normalize)
