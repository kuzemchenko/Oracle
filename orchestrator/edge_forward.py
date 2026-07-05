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
  • активация ТОЛЬКО при сигнале — |неотыгранный edge| ≥ NOISE_FLOOR_SIGMA_FRAC × σ_h терминала;
    печатать все рёбра ежедневно = разбавлять BSS безнаправленным шумом p≈0.5 (ребро никогда
    не докажет скилл). Условие детерминировано и фиксируется ДО исхода — селекция по информации,
    доступной в момент прогноза, П16 чиста. Пропуски журналируются с причиной (П8).
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
from orchestrator import progress as PROG             # noqa: E402
from mathlib import cascade as CAS                    # noqa: E402
from mathlib import sealing as SEAL                   # noqa: E402
from mathlib.calibration import forward_promotion as FP   # noqa: E402

DB = ROOT / "storage" / "oracle.db"
SENS = ROOT / "knowledge" / "cascade_sensitivities.yaml"
LOGS = ROOT / "journal" / "edge_forward_logs"

KIND = "edge_forward"
NOISE_FLOOR_SIGMA_FRAC = 0.5     # подпись 05.07: |edge| ≥ 0.5·σ_h терминала, иначе ребро «спит»
MAX_SEALS_PER_RUN = 40           # кэп-гигиена: библиотека сейчас ~24 ребра; рост — сигнал разобраться
# Дедуп ставки: identity трека (FC.DEDUP_FIELDS уже включает kind, B4 05.07) + edge_key: два РАЗНЫХ
# ребра к одному терминалу (напр. USO→BNO и DBC→BNO при равных порогах) дают РАЗНЫЕ прогнозы — у
# каждого своя атрибуция промоушена, дедупить их между собой нельзя.
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
    итоги = {"рёбер_в_библиотеке": len(edges), "запечатано": 0, "спит_под_порогом": 0,
             "нет_шока_источника": 0, "нет_edge_на_узле": 0, "узел_не_построен": 0,
             "не_запечатываемо_§9": 0, "дубль_пропущен": 0, "кэп_отброшено": 0}
    детали = []
    try:
        for edge in edges:
            rec = {"ребро": FP.edge_key(edge["from"], edge["to"], edge["lag"])}
            shock = _shock(con, edge["from"], asof)
            if shock is None:
                итоги["нет_шока_источника"] += 1
                детали.append({**rec, "статус": "пропуск", "причина": "нет баров окна шока (П8)"})
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
            fact = GB.node_to_facts({**узлы[0], "root": edge["from"]},
                                    con=con, horizon_days=horizon)
            amp, sigma_h = fact.get("amplitude"), fact.get("sigma_h")
            if amp in (None, 0):
                итоги["нет_edge_на_узле"] += 1
                детали.append({**rec, "статус": "пропуск", "shock": round(shock, 5),
                               "причина": узлы[0].get("причина") or "неотыгранный edge отсутствует"})
                continue
            if not sigma_h or abs(amp) < noise_floor_frac * sigma_h:
                итоги["спит_под_порогом"] += 1
                детали.append({**rec, "статус": "спит", "shock": round(shock, 5),
                               "edge": round(amp, 5), "sigma_h": (round(sigma_h, 5) if sigma_h else None),
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
            spec["spec_ref"] = "§R4.5 B4 форвард-тест рёбер; " + spec.get("spec_ref", "")
            if итоги["запечатано"] >= max_seals:
                итоги["кэп_отброшено"] += 1
                детали.append({**rec, "статус": "пропуск",
                               "причина": f"кэп {max_seals} прогнозов/прогон (гигиена) — НЕ запечатано"})
                continue
            if seal:
                sealed = SEAL.seal(spec, path=predictions_path, dedup_fields=DEDUP_FIELDS)
                if sealed is None:
                    итоги["дубль_пропущен"] += 1
                    детали.append({**rec, "статус": "дубль", "причина": "та же ставка уже в журнале"})
                    continue
            итоги["запечатано"] += 1
            детали.append({**rec, "статус": ("запечатано" if seal else "к_печати (dry)"),
                           "актив": spec["asset"], "направление": spec["direction"],
                           "порог": spec["threshold"], "resolve_by": spec["resolve_by"],
                           "вероятность": spec.get("probability"), "edge": round(amp, 5),
                           "sigma_h": round(sigma_h, 5), "shock": round(shock, 5)})
    finally:
        if own:
            con.close()

    protocol = {
        "run_id": run_id, "ts": now_dt.isoformat(timespec="seconds"), "asof": asof,
        "режим": "B4 форвард-тест рёбер (§R4.5)", "kind": KIND, "seal": bool(seal),
        "порог_активации": f"|edge| ≥ {noise_floor_frac}·σ_h (подпись 05.07)",
        "итоги": итоги, "рёбра": детали,
        "spec_ref": "§R4.5; §9/П16 запечатывание; §10 форвард-промоушен; Инв#6 (LLM нет)",
    }
    if write:
        LOGS.mkdir(parents=True, exist_ok=True)
        PROG.atomic_write_text(LOGS / f"{run_id}.json",
                               json.dumps(protocol, ensure_ascii=False, indent=2))
    return protocol
