# -*- coding: utf-8 -*-
"""orchestrator/edge_forward.py — B4 (§R4.5): ежедневный детерминированный форвард-тест ВСЕХ рёбер.

Библиотека рёбер (knowledge/cascade_sensitivities.yaml: sensitivities + chain_sensitivities,
направленные пары источник→узел с лагом) ежедневно проверяется на активацию: у источника есть
шок за окно реакции (§R2.1), перенос даёт неотыгранный ход на узле НЕ МЕНЬШЕ порога шума.
Активированное ребро запечатывается ОДНОЗВЕННЫМ §9-прогнозом (kind='edge_forward') — исход
чисто атрибутируется ребру и кормит форвард-промоушен (mathlib.calibration.forward_promotion,
§10: N≥30 + значимый скилл над базой). Это «фарм-система»: ребро зарабатывает ярус-A форвардом,
а не исторической бетой (П16).

Подписи владельца 05.07.2026 (сессия):
  • активация ТОЛЬКО при сигнале. После гейта stage-review B4 сигнал = ТРИ условия разом
    (все детерминированы и фиксируются ДО исхода — П16 чиста; пропуски с причиной — П8):
      1) шок ИСТОЧНИКА над его шумом: |shock| ≥ SHOCK_SIGMA_FRAC × σ_источника × √окна —
         иначе «активацией» считался бы любой ненулевой тик (блокер гейта: при тихом источнике
         edge ≈ −realized терминала, т.е. ставка на РЕВЕРС собственного хода терминала,
         отравляющая промоушен-атрибуцию ребра статистикой mean-reversion);
      2) неотыгранный ход В СТОРОНУ каскада (P1#5): unpriced_fraction > 0 — реверс/шумовая
         амплитуда (|amplitude_total| < UNPRICED_MIN_AMP → доля не измерима) НЕ печатается;
      3) |неотыгранный edge| ≥ NOISE_FLOOR_SIGMA_FRAC × σ_h терминала — иначе безнаправленный
         шум p≈0.5 разбавляет BSS и ребро никогда не докажет скилл.
    Плюс КУЛДАУН (гейт B4, серийная псевдорепликация): пока по ребру есть НЕразрешённый прогноз
    (resolve_by в будущем), новый не печатается — эпизод шока живёт в 5-барном окне, и без
    кулдауна один эпизод давал бы ~5 коррелированных «независимых» исходов в биномтест §10.
  • герметичность: kind='edge_forward' — СВОЙ третий трек табло (resolve сегментирует отдельно);
    к денежному Brier/§11 не приближается (MONEY_EDGE_KINDS не трогается), в провизорный трек
    выдачи не подмешивается (разные популяции — идеи выдачи vs механический фарм-поток).

Порог NOISE_FLOOR_SIGMA_FRAC живёт здесь константой, НЕ в config/thresholds.yaml — тот файл
СГЕНЕРИРОВАН калибровкой §23.1 и перезаписывается; правка порога = правка кода с ревью (§25).

Только детерминированный код (Инв#6): LLM не вызывается, стоимость ~$0. Бюджет-гард не нужен.
Расчёт узла — та же боевая механика, что в event_first: cascade_build.build_from_db (asof-гейт
П16, adjusted_close, отыгранное, forward_promotions) → graph_build.node_to_facts →
cascade_resolve.seal_spec. Псевдо-цепочка из 2 узлов ⇒ path_edges длины 1 (атрибуция ребру).
"""
import datetime
import json
import pathlib
import sqlite3
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import cascade_build as CB          # noqa: E402
from orchestrator import graph_build as GB            # noqa: E402
from orchestrator import cascade_resolve as CR        # noqa: E402
from orchestrator import forecast as FC               # noqa: E402
from orchestrator import resolve as RES               # noqa: E402  (track_for_kind: герметичность треков)
from orchestrator import progress as PROG             # noqa: E402
from mathlib import cascade as CAS                    # noqa: E402
from mathlib import sealing as SEAL                   # noqa: E402
from mathlib.calibration import forward_promotion as FP   # noqa: E402

DB = ROOT / "storage" / "oracle.db"
SENS = ROOT / "knowledge" / "cascade_sensitivities.yaml"
LOGS = ROOT / "journal" / "edge_forward_logs"

KIND = "edge_forward"
NOISE_FLOOR_SIGMA_FRAC = 0.5     # подпись 05.07: |edge| ≥ 0.5·σ_h терминала, иначе ребро «спит»
SHOCK_SIGMA_FRAC = 0.5           # гейт B4: |shock источника| ≥ 0.5·σ_ист·√окна, иначе шока НЕТ
SIGMA_BARS = 61                  # окно дневной σ — как vol_fn боевого строителя (соосность)
MAX_SEALS_PER_RUN = 40           # кэп-гигиена: библиотека сейчас ~24 ребра; рост — сигнал разобраться
# Дедуп ставки: identity трека (FC.DEDUP_FIELDS: track+ставка, stage-review B4) + edge_key: два
# РАЗНЫХ ребра к одному терминалу (напр. USO→BNO и DBC→BNO при равных порогах) дают РАЗНЫЕ
# прогнозы — у каждого своя атрибуция промоушена, дедупить их между собой нельзя.
DEDUP_FIELDS = ("edge_key",) + FC.DEDUP_FIELDS


def edge_library(path=SENS):
    """Направленные рёбра библиотеки: (from, to, lag) из sensitivities + chain_sensitivities,
    дедуп по тройке. Порядок детерминирован (сортировка) — прогон воспроизводим."""
    doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    entries = list(doc.get("sensitivities") or []) + list(doc.get("chain_sensitivities") or [])
    seen, edges = set(), []
    for e in entries:
        up, down = e.get("источник"), e.get("узел")
        if not up or not down or up == down:
            continue
        lag = int(e.get("lag") or 0)
        key = (up, down, lag)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"from": up, "to": down, "lag": lag})
    edges.sort(key=lambda x: (x["from"], x["to"], x["lag"]))
    return edges


def _shock(con, symbol, asof):
    """Шок источника за окно реакции §R2.1 — как event_first._window_return: adjusted_close
    (F0#8, корпдействия) + asof-гейт date<=asof (П16, случайный будущий бар не течёт в прогноз)."""
    rows = con.execute(
        "SELECT COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
        "AND COALESCE(adjusted_close, close) IS NOT NULL AND date <= ? "
        "ORDER BY date DESC LIMIT ?", (symbol, asof, CAS.EVENT_WINDOW_DAYS + 1)).fetchall()
    px = [float(r[0]) for r in rows][::-1]
    return CAS.window_return(px)


def _pseudo_chain(edge):
    """Ребро → минимальная 2-узловая цепочка для боевого строителя (path_edges длины 1)."""
    return {"id": f"b4:{edge['from']}->{edge['to']}@lag{edge['lag']}",
            "nodes": [{"order": 1, "node": edge["from"], "instruments": [edge["from"]]},
                      {"order": 2, "node": edge["to"], "instruments": [edge["to"]]}],
            "edges": [{"from": 1, "to": 2, "lag_days": edge["lag"]}]}


def _sigma_daily(con, symbol, asof):
    """Дневная σ лог-доходностей символа (окно SIGMA_BARS, adjusted_close, asof-гейт) —
    шумовой пол ИСТОЧНИКА для гейта величины шока (stage-review B4). None — не измерима (П8)."""
    rows = con.execute(
        "SELECT COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
        "AND COALESCE(adjusted_close, close) IS NOT NULL AND date <= ? "
        "ORDER BY date DESC LIMIT ?", (symbol, asof, SIGMA_BARS)).fetchall()
    px = [float(r[0]) for r in rows][::-1]
    r = CAS.log_returns(px)
    return float(r.std()) if r.size >= 2 else None


def _edges_on_cooldown(predictions_path, now_iso):
    """Рёбра с ОТКРЫТЫМ (resolve_by в будущем) edge_forward-прогнозом — новый не печатаем
    (кулдаун: один открытый прогноз на ребро, защита биномтеста §10 от серийных дублей эпизода)."""
    try:
        preds = SEAL.read_predictions(predictions_path) if predictions_path \
            else SEAL.read_predictions()
    except FileNotFoundError:
        return set()
    return {p.get("edge_key") for p in preds
            if p.get("kind") == KIND and p.get("edge_key")
            and str(p.get("resolve_by") or "") > now_iso}


def run_edge_forward(*, write=True, seal=True, con=None, now_dt=None,
                     sens_path=SENS, db=None, predictions_path=None,
                     noise_floor_frac=NOISE_FLOOR_SIGMA_FRAC, max_seals=MAX_SEALS_PER_RUN):
    """Один суточный проход по библиотеке рёбер. Возвращает протокол; seal=False — сухой прогон
    (журнал прогнозов НЕ трогается), write=False — и протокол на диск не пишется.

    Горизонт ребра = lag + окно реакции (перенос доезжает через лаг, затем меряем реакцию §R2.1) —
    в торговых днях; resolve_by считает forecast._resolve_by (календарная поправка ×7/5)."""
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    asof = now_dt.strftime("%Y-%m-%d")
    run_id = "b4_" + now_dt.strftime("%Y%m%dT%H%M%SZ")
    edges = edge_library(sens_path)
    own = con is None
    if con is None:
        con = sqlite3.connect(str(db or DB), timeout=30)
    итоги = {"рёбер_в_библиотеке": len(edges), "запечатано": 0, "кулдаун_pending": 0,
             "шок_под_порогом": 0, "реверс_или_шум_P1#5": 0, "спит_под_порогом": 0,
             "нет_шока_источника": 0, "σ_не_измерима": 0, "нет_edge_на_узле": 0,
             "узел_не_построен": 0, "не_запечатываемо_§9": 0, "дубль_пропущен": 0,
             "кэп_отброшено": 0}
    детали = []
    now_iso = now_dt.isoformat(timespec="seconds")
    кулдаун = _edges_on_cooldown(predictions_path, now_iso)   # и в dry: зеркалит боевое поведение
    try:
        for edge in edges:
            rec = {"ребро": FP.edge_key(edge["from"], edge["to"], edge["lag"])}
            if rec["ребро"] in кулдаун:
                итоги["кулдаун_pending"] += 1
                детали.append({**rec, "статус": "кулдаун",
                               "причина": "открытый прогноз ребра ещё не разрешён — эпизод шока "
                                          "не плодит серийных дублей в биномтест §10"})
                continue
            shock = _shock(con, edge["from"], asof)
            if shock is None:
                итоги["нет_шока_источника"] += 1
                детали.append({**rec, "статус": "пропуск", "причина": "нет баров окна шока (П8)"})
                continue
            # гейт B4 (блокер stage-review): величина шока ИСТОЧНИКА над его собственным шумом.
            # Без него «активацией» был любой ненулевой тик, а edge ≈ −realized терминала —
            # ставка на реверс чужого хода, отравляющая атрибуцию ребра.
            σ_src = _sigma_daily(con, edge["from"], asof)
            if not σ_src:
                итоги["σ_не_измерима"] += 1
                детали.append({**rec, "статус": "пропуск", "shock": round(shock, 5),
                               "причина": "σ источника не измерима (П8) — порог шока не построить"})
                continue
            порог_шока = SHOCK_SIGMA_FRAC * σ_src * (CAS.EVENT_WINDOW_DAYS ** 0.5)
            if abs(shock) < порог_шока:
                итоги["шок_под_порогом"] += 1
                детали.append({**rec, "статус": "спит", "shock": round(shock, 5),
                               "порог_шока": round(порог_шока, 5),
                               "причина": f"|shock| < {SHOCK_SIGMA_FRAC}·σ_ист·√окна — шока нет"})
                continue
            horizon = int(edge["lag"]) + CAS.EVENT_WINDOW_DAYS
            built = CB.build_from_db(_pseudo_chain(edge), shock, horizon_days=horizon,
                                     con=con, db=db, asof=asof)
            узлы = built.get("узлы") or []
            if not узлы:
                итоги["узел_не_построен"] += 1
                детали.append({**rec, "статус": "пропуск", "shock": round(shock, 5),
                               "причина": "терминал без данных/истории (П8)"})
                continue
            node = узлы[0]
            # гейт P1#5 (блокер stage-review): неотыгранный ход обязан быть В СТОРОНУ каскада.
            # unpriced_fraction≤0 = реверс/переотыграно; None = |amplitude_total|<UNPRICED_MIN_AMP —
            # предсказанное движение на уровне шума, доля не измерима. Оба случая НЕ печатаются.
            frac = (node.get("edge") or {}).get("unpriced_fraction")
            if frac is None or frac <= 0:
                итоги["реверс_или_шум_P1#5"] += 1
                детали.append({**rec, "статус": "пропуск", "shock": round(shock, 5),
                               "unpriced_fraction": frac,
                               "причина": node.get("причина")
                               or "нет неотыгранного хода в сторону каскада (P1#5)"})
                continue
            fact = GB.node_to_facts({**node, "root": edge["from"]},
                                    con=con, horizon_days=horizon)
            amp, sigma_h = fact.get("amplitude"), fact.get("sigma_h")
            if amp in (None, 0):
                итоги["нет_edge_на_узле"] += 1
                детали.append({**rec, "статус": "пропуск", "shock": round(shock, 5),
                               "причина": node.get("причина") or "неотыгранный edge отсутствует"})
                continue
            if not sigma_h:
                итоги["σ_не_измерима"] += 1
                детали.append({**rec, "статус": "пропуск", "shock": round(shock, 5),
                               "причина": "σ_h терминала не измерима (П8) — порог сигнала не построить"})
                continue
            if abs(amp) < noise_floor_frac * sigma_h:
                итоги["спит_под_порогом"] += 1
                детали.append({**rec, "статус": "спит", "shock": round(shock, 5),
                               "edge": round(amp, 5), "sigma_h": round(sigma_h, 5),
                               "причина": f"|edge| < {noise_floor_frac}·σ_h (подпись 05.07)"})
                continue
            spec = CR.seal_spec(fact, kind=KIND, run_id=run_id, horizon_days=horizon,
                                con=con, now_dt=now_dt)
            if spec is None:
                итоги["не_запечатываемо_§9"] += 1
                детали.append({**rec, "статус": "пропуск", "shock": round(shock, 5),
                               "причина": "seal_spec: нет цены/edge — §9-спека не собралась (П8)"})
                continue
            spec["edge_key"] = rec["ребро"]           # атрибуция ребру явным полем (+ в дедуп-identity)
            spec["track"] = RES.track_for_kind(KIND)  # внутри-трековый дедуп (stage-review B4)
            spec["spec_ref"] = "§R4.5 B4 форвард-тест рёбер; " + spec.get("spec_ref", "")
            if итоги["запечатано"] >= max_seals:
                итоги["кэп_отброшено"] += 1
                детали.append({**rec, "статус": "пропуск",
                               "причина": f"кэп {max_seals} прогнозов/прогон (гигиена) — НЕ запечатано"})
                continue
            if seal:
                sealed = SEAL.seal(spec, path=predictions_path, dedup_fields=DEDUP_FIELDS,
                                   dedup_normalize=FC.dedup_normalize)
                if sealed is None:
                    итоги["дубль_пропущен"] += 1
                    детали.append({**rec, "статус": "дубль", "причина": "та же ставка уже в журнале"})
                    continue
            итоги["запечатано"] += 1
            детали.append({**rec, "статус": ("запечатано" if seal else "к_печати (dry)"),
                           "актив": spec["asset"], "направление": spec["direction"],
                           "порог": spec["threshold"], "resolve_by": spec["resolve_by"],
                           "вероятность": spec.get("probability"), "edge": round(amp, 5),
                           "sigma_h": round(sigma_h, 5), "shock": round(shock, 5),
                           "unpriced_fraction": frac})
    finally:
        if own:
            con.close()

    protocol = {
        "run_id": run_id, "ts": now_iso, "asof": asof,
        "режим": "B4 форвард-тест рёбер (§R4.5)", "kind": KIND, "seal": bool(seal),
        "порог_активации": (f"|shock| ≥ {SHOCK_SIGMA_FRAC}·σ_ист·√окна И unpriced_fraction>0 (P1#5) "
                            f"И |edge| ≥ {noise_floor_frac}·σ_h (подписи 05.07 + гейт stage-review B4)"),
        "кулдаун": "один открытый прогноз на ребро (серийная псевдорепликация эпизода — гейт B4)",
        "итоги": итоги, "рёбра": детали,
        "spec_ref": "§R4.5; §9/П16 запечатывание; §10 форвард-промоушен; Инв#6 (LLM нет)",
    }
    if write:
        LOGS.mkdir(parents=True, exist_ok=True)
        PROG.atomic_write_text(LOGS / f"{run_id}.json",
                               json.dumps(protocol, ensure_ascii=False, indent=2))
    return protocol
