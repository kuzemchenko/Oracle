# -*- coding: utf-8 -*-
"""orchestrator/resolve.py — сверка исходов запечатанных прогнозов (§10.10, §4 «Разборщик», §16).

Берёт запечатанные прогнозы (journal/predictions.jsonl, НЕИЗМЕНЯЕМЫЕ — П16), у которых вышел
срок (resolve_by), тянет фактический close цены-сверки из storage/oracle.db и выносит исход
0/1 ЧИСТЫМ кодом (mathlib.outcomes). Пока срока нет или нет данных — статус pending, исход НЕ
выдумывается (П8).

КУДА пишет: исходы идут в ОТДЕЛЬНЫЙ журнал journal/outcomes.jsonl (append-only), привязка к
прогнозу по hash. predictions.jsonl НЕ редактируется (запечатанный журнал, инвариант 3) —
исход не может «переписать» прогноз задним числом, только дополнить его связкой по hash.

Brier (mathlib.brier) считается по join прогноз↔исход — это вход дашборда §15 и петли §25.
"""
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mathlib import sealing as SEAL          # noqa: E402
from mathlib import outcomes as OUT          # noqa: E402
from mathlib import brier as BR              # noqa: E402

DB = ROOT / "storage" / "oracle.db"
OUTCOMES_PATH = ROOT / "journal" / "outcomes.jsonl"

# ГЕРМЕТИЧНОСТЬ ТРЕКОВ (B3c §R3, Вариант 2): провизорный трек (ярус B/C — гипотезы) копит СВОЙ Brier
# и НЕ приближается к денежным воротам §11. Эти kind исключаются из денежного Brier и гейта-270.
PROVISIONAL_KINDS = ("cascade_provisional",)
# F0#6: ГЕРМЕТИЧНОСТЬ §11 через АЛОУЛИСТ — в денежный трек/гейт-270 идут ТОЛЬКО реальные edge-прогнозы.
# Раньше money_out = «всё, что не провизорное» → calibration (baseline-монетка P=0.5, kind='calibration')
# протекала в денежный Brier и до_ворот_270 (96/96 исходов = calibration). Калибровка — свой трек.
MONEY_EDGE_KINDS = ("funnel_forward", "theme_daily", "cascade_money")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_outcomes(path=None):
    path = pathlib.Path(path) if path else OUTCOMES_PATH
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _observed_close_after(con, symbol, resolve_by_date):
    """Первый close НА или ПОСЛЕ даты resolve_by. Возвращает (observed_value, observed_at) или (None,None).
    observed_at = дата close + T20:00 UTC (закрытие US-сессии) — чтобы быть ≥ resolve_by при сверке."""
    row = con.execute(
        "SELECT date, close FROM quotes WHERE symbol=? AND date>=? AND close IS NOT NULL "
        "ORDER BY date ASC LIMIT 1", (symbol, resolve_by_date)).fetchone()
    if not row:
        return None, None
    observed_at = f"{row[0]}T20:00:00+00:00"
    return float(row[1]), observed_at


def run_resolve(write=True, predictions_path=None, outcomes_path=None):
    """Сверить все «дозревшие» прогнозы. Возвращает сводку; новые исходы пишет в outcomes.jsonl."""
    preds = SEAL.read_predictions(predictions_path)
    done = {o["hash"] for o in read_outcomes(outcomes_path) if o.get("hash")}
    opath = pathlib.Path(outcomes_path) if outcomes_path else OUTCOMES_PATH

    con = sqlite3.connect(DB) if DB.exists() else None
    newly, still_pending, errors = [], 0, []
    try:
        for p in preds:
            h = p.get("hash")
            if not h or h in done:
                continue                       # уже сверён — журнал исходов append-only
            asset = p.get("asset")
            resolve_by = str(p.get("resolve_by", ""))
            obs_val, obs_at = (None, None)
            if con is not None and asset and resolve_by:
                obs_val, obs_at = _observed_close_after(con, asset, resolve_by[:10])
            res = OUT.resolve_prediction(p, obs_val, obs_at)
            if res["status"] == "resolved":
                rec = {
                    "hash": h, "asset": asset, "kind": p.get("kind"),
                    "direction": res["direction"], "threshold": res["threshold"],
                    "resolve_by": resolve_by, "probability": res.get("probability"),
                    "observed_value": res["observed_value"], "observed_at": res["observed_at"],
                    "outcome": res["outcome"], "resolved_at": _now_iso(),
                    "spec_ref": "§10.10 сверка исходов; §16 форвард-онли",
                }
                newly.append(rec)
            elif res["status"] == "error":
                errors.append({"hash": h[:12], "asset": asset, "error": res.get("error")})
            else:
                still_pending += 1
    finally:
        if con is not None:
            con.close()

    if write and newly:
        opath.parent.mkdir(parents=True, exist_ok=True)
        with open(opath, "a", encoding="utf-8") as f:     # ТОЛЬКО append
            for rec in newly:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Brier по ВСЕМ сверенным исходам (старые + новые). Если записали — перечитываем журнал
    # (он уже содержит newly); если write=False — склеиваем в памяти.
    all_out = read_outcomes(outcomes_path) if (write and newly) else (read_outcomes(outcomes_path) + newly)

    # B3c: денежный Brier + гейт-270 — ТОЛЬКО не-провизорные; провизорный трек копит свой Brier ОТДЕЛЬНО
    # и к §11 не приближается (герметичность Варианта 2).
    def _bins(lst):
        pr = [float(o["probability"]) for o in lst
              if o.get("probability") is not None and o.get("outcome") in (0, 1)]
        ou = [int(o["outcome"]) for o in lst
              if o.get("probability") is not None and o.get("outcome") in (0, 1)]
        return pr, ou

    money_out = [o for o in all_out if o.get("kind") in MONEY_EDGE_KINDS]   # F0#6 алоулист
    prov_out = [o for o in all_out if o.get("kind") in PROVISIONAL_KINDS]
    m_probs, m_outs = _bins(money_out)
    p_probs, p_outs = _bins(prov_out)
    brier = BR.brier_score(m_probs, m_outs) if m_probs else None
    band = BR.calibration_band_pp(m_probs, m_outs) if m_probs else None
    prov_brier = BR.brier_score(p_probs, p_outs) if p_probs else None

    return {
        "прогнозов_в_журнале": len(preds),
        "сверено_сейчас": len(newly),
        "ещё_pending": still_pending,
        "ошибок": errors,
        "всего_исходов": len(all_out),
        "brier": (None if brier is None else round(brier, 4)),                 # ДЕНЕЖНЫЙ трек (§11)
        "калибровка_band_пп": (None if band is None else round(band, 2)),
        # F0#7: гейт-270 считаем по ТОМУ ЖЕ подмножеству, что Brier (probability присутствует) —
        # иначе ворота «созревают» на неизмеримых прогнозах (probability=None в гейт шёл, в Brier нет).
        "до_ворот_270": max(0, 270 - len(m_probs)),                            # только измеримые денежные → §11
        "провизорный_трек": {"исходов": len(prov_out),                         # отдельный Brier, НЕ в §11
                             "brier": (None if prov_brier is None else round(prov_brier, 4))},
        "spec_ref": "§10.10, §16, §11 ворота; B3c герметичность треков; скилл calibrate п.5",
    }
