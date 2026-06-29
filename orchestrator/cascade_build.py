# -*- coding: utf-8 -*-
"""orchestrator/cascade_build.py — сборка узлов-КОМПАНИЙ из АВТОРСКОЙ каскадной цепочки (Этап 4 §3c).

Резолв идёт в КОНКРЕТНУЮ компанию ниже по каскаду (CLF, GEV…), НЕ в proxy_etf темы (D1). Источник
шока (якорь цепочки, напр. VRT) — лишь корень; торгуемый edge — на дальних чокпоинт-узлах (§5/П5).

Покозвенный прогноз (§3c, инвариант 6 — свёртку считает КОД):
  • звено upstream→down: ярус A, если бета пинится на лету (Этап 3); иначе механизм-гипотеза карты
    (ярус C — низкая надёжность, широкая полоса) — research-only, без seal (П16);
  • compose_chain свёртывает путь корень→узел (амплитуда + проброс дисперсии + надёжность);
  • cascade_edge = амплитуда − уже отыгранное на терминале → ранг по |edge|×надёжность.

Доступ к данным инъектируется (sensitivity_fn/realized_fn/vol_fn/has_data_fn) — модуль тестируется
без БД; build_from_db() подключает боевые источники из storage/oracle.db.
"""
import math
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import cascade as CAS                       # noqa: E402
from mathlib.calibration import sensitivity as SEN       # noqa: E402
from mathlib.calibration import forward_promotion as FP   # noqa: E402

CHAINS = ROOT / "knowledge" / "cascade_chains.yaml"
PROMOTIONS = ROOT / "knowledge" / "forward_promotions.yaml"
MIN_BARS = 60
R2_PIN_CAP = 0.95   # P1#6: верхний кап надёжности пина — r²≈1.0 на рынках = артефакт, не идеальная связь


def load_chains(path=CHAINS):
    return (yaml.safe_load(open(path, encoding="utf-8")) or {}).get("chains", [])


def load_promotions(path=PROMOTIONS):
    """Форвард-промоушены рёбер (ops/promote_edges.py --apply). {edge_key: запись}. Нет файла → {}.
    Применяются только записи с promote=True (драйвер пишет именно их, но фильтруем на всякий)."""
    if not pathlib.Path(path).exists():
        return {}
    doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    return {k: v for k, v in (doc.get("promotions") or {}).items() if (v or {}).get("promote")}


def chains_for_source(src, chains=None):
    """Авторские цепочки, чей ЯКОРЬ (узел минимального порядка) = источник шока src.

    Источник — корень каскада; вниз по цепочке резолвим в компании. Сравнение по инструментам узла.
    """
    out = []
    for c in (chains if chains is not None else load_chains()):
        nodes = sorted((c.get("nodes") or []), key=lambda n: n.get("order", 0))
        if not nodes:
            continue
        anchor = nodes[0]
        if src in (anchor.get("instruments") or []):
            out.append(c)
    return out


def _rep_with_data(node, has_data_fn):
    """Представительный торгуемый инструмент узла, у которого ЕСТЬ данные (иначе None — П8)."""
    for inst in (node.get("instruments") or []):
        if has_data_fn(inst):
            return inst
    return None


def _link_from_sensitivity(up, down, lag, rec, promotions=None):
    """Звено по ярусам §3c: исторический пин → A; иначе ФОРВАРД-промоушен ребра → A (заработан на
    запечатанных исходах, §10); иначе механизм-гипотеза карты (C, research-only)."""
    if rec and rec.get("pinned") and rec.get("beta_pinned") is not None:
        beta = float(rec["beta_pinned"])
        sd = round(abs(beta) * max(float(rec.get("rel_dispersion") or 0.1), 0.05), 6)
        # P1#6: кап r² — даже пин не бывает «идеальным» на реальных рынках; r²≈1.0 = артефакт
        # (короткая/вырожденная история). Ограничиваем сверху, чтобы фиктивная уверенность не текла.
        rel = min(float(rec.get("r2_fullsample") or 0.5), R2_PIN_CAP)
        return {"tier": "A", "gain": beta, "gain_sd": sd,
                "reliability": round(rel, 4),
                "lag": int(lag), "established": True,
                "провенанс": f"ярус A (пин β={round(beta,4)}): {up}→{down}"}
    # ФОРВАРД-промоушен (решение 28.06): ребро заработало ярус A корректными запечатанными форвард-
    # прогнозами (N≥30, значимый скилл, §10). Перенос ДОКАЗАН форвардом; величину берём точечной.
    prom = (promotions or {}).get(FP.edge_key(up, down, lag)) if promotions else None
    if prom and prom.get("promote"):
        # F0#5/§2.6: форвард доказывает ПЕРЕНОС (направление), НЕ величину. Берём точечную бету из
        # промоушена/sensitivity; если величина НЕИЗВЕСТНА — НЕ фабрикуем gain=1.0 в money-трек
        # (это уходило в amplitude/edge_rank как реальная амплитуда). Без величины → механизм (research).
        beta = prom.get("beta_fullsample")
        if beta is None:
            beta = (rec or {}).get("beta_fullsample")
        if beta is not None:
            return {"tier": "A", "gain": float(beta),
                    "gain_sd": round(abs(float(beta)) * 0.5, 6),     # форвард доказал перенос, не величину
                    "reliability": round(float(prom.get("reliability") or 0.0), 4),
                    "lag": int(lag), "established": True,
                    "провенанс": (f"ярус A (форвард-промоушен): {up}→{down} — "
                                  f"N={prom.get('n')}, hit-rate {prom.get('hit_rate')}, "
                                  f"BSS {prom.get('bss')} над базой {prom.get('p0')} (§10)")}
        # величина неизвестна → падаем в механизм-гипотезу ниже (research-only, без фикции 1.0)
    # не пинится / нет данных → механизм-гипотеза карты (низкая надёжность, широкая полоса)
    prior, why = 1.0, "бета не пинится (Этап 3) → механизм карты"
    if rec and rec.get("beta_fullsample") is not None:
        prior = float(rec["beta_fullsample"])
        why = f"fullsample β={round(prior,3)} не устойчива → механизм"
    return CAS.link_mechanism(prior, lag=int(lag), провенанс=f"{up}→{down}: {why}")


def build_chain_nodes(chain, shock0, *, horizon_days,
                      sensitivity_fn, realized_fn, vol_fn, has_data_fn, shock0_sd=0.0,
                      promotions=None):
    """Авторская цепочка + корневой шок → ранжированные узлы-КОМПАНИИ (совместимы с resolve_node).

    amplitude узла для резолва = НЕпрокинутый edge (расчётная амплитуда − уже отыгранное на терминале);
    sealable = sealable_path свёртки (все звенья A и перенос установлен). Ранг = |edge|×надёжность.
    """
    nodes = sorted((chain.get("nodes") or []), key=lambda n: n.get("order", 0))
    edges = {(e.get("from"), e.get("to")): e for e in (chain.get("edges") or [])}
    reps = {n.get("order"): (_rep_with_data(n, has_data_fn), n) for n in nodes}
    # УНИКАЛЬНЫЕ порядки по возрастанию — звенья строим по ним (как edges картографа), а НЕ zip по
    # сырому списку с дублями. Картограф (event_mapping._proposal_to_chain) может дать несколько узлов
    # ОДНОГО порядка; reps дедупит по order, но zip(orders, orders[1:]) на [1,2,2,3] рождал само-звено
    # order_i→order_i → on_the_fly(X,X) → β=1, r²=1.0, ФИКТИВНЫЙ ярус-A, забивавший money-трек. Дедуп
    # по uorders + гард up==down это убивает (П8: само-звено — это не связь, а артефакт).
    uorders = sorted(reps.keys())

    # последовательные звенья order_i → order_{i+1}
    links_seq = []
    links_meta = []                              # (up_sym, down_sym, lag) для path_edges (форвард-атрибуция)
    for a, b in zip(uorders, uorders[1:]):
        up, down = reps[a][0], reps[b][0]
        if up is None or down is None or up == down:
            links_seq.append(None)
            links_meta.append(None)
            continue
        lag = int((edges.get((a, b)) or {}).get("lag_days") or 0)
        links_seq.append(_link_from_sensitivity(up, down, lag, sensitivity_fn(up, down, lag),
                                                promotions=promotions))
        links_meta.append((up, down, lag))

    out = []
    for idx in range(1, len(uorders)):           # терминал = uorders[idx]; путь = links_seq[:idx]
        order = uorders[idx]
        down, node = reps[order]
        if down is None:
            continue                             # нет инструмента с данными — узел не резолвится (П8)
        path = links_seq[:idx]
        path_meta = links_meta[:idx]
        # path_edges: рёбра пути для ФОРВАРД-атрибуции исхода (forward_promotion). Однозвенный путь
        # (len==1, order-2 узел) → бинарный исход терминала чисто измеряет это ребро.
        path_edges = [{"from": m[0], "to": m[1], "lag": m[2],
                       "tier": (l or {}).get("tier"), "beta_fullsample": (l or {}).get("gain")}
                      for m, l in zip(path_meta, path) if m is not None]
        comp = CAS.compose_chain(path, shock0, shock0_sd=shock0_sd)
        amp_total = comp.get("amplitude")
        amp_sd = comp.get("amplitude_sd") or 0.0
        realized = realized_fn(down, horizon_days) or 0.0
        edge = CAS.cascade_edge(amp_total, realized, amplitude_sd=amp_sd) if amp_total is not None else None
        sigma = vol_fn(down) or 0.0
        # P1#4: вероятность с НЕопределённостью сноса (amp_sd, проброс дисперсии) и сжатием к 0.5 по
        # надёжности связи — иначе слабое звено (r²≈0.04) давало уверенность 0.99 (артефакт).
        prob = (CAS.node_probability(edge["edge"], sigma, horizon_days, 0.0,
                                     amplitude_sd=amp_sd, reliability=comp.get("reliability"))
                if (edge and sigma > 0) else None)
        # P1#5: money-идея ТОЛЬКО если есть неотыгранный ход В СТОРОНУ каскада (unpriced_fraction>0 и
        # |amplitude|≥порога шума). Иначе edge держится на −realized — это ставка ПРОТИВ недавнего хода
        # (мин-реверс на уровне шума), не каскадная возможность → research-only (не в money/seal).
        frac = (edge or {}).get("unpriced_fraction")
        genuine_unpriced = (frac is not None and frac > 0)
        out.append({
            "узел": down, "order": order, "chokepoint": bool(node.get("chokepoint")),
            "amplitude": (edge["edge"] if edge else None),       # резолв по НЕпрокинутому edge
            "amplitude_total": amp_total,
            "sealable": bool(comp.get("sealable_path")),
            "probability": prob,
            "reliability_r2": comp.get("reliability"),
            "lowest_tier": comp.get("lowest_tier"),
            "tiers": comp.get("tiers"),
            "lag_total": comp.get("lag_total"),       # суммарное окно входа (для ворот §R2)
            "edge": edge,
            "edge_rank": CAS.edge_rank_score(edge["edge"] if edge else None, comp.get("reliability")),
            "research": (not bool(comp.get("sealable_path"))) or (not genuine_unpriced),
            "причина": (comp.get("причина_seal") if genuine_unpriced else
                       "research-only: нет неотыгранного хода в сторону каскада (edge — реверс/шум, P1#5)"),
            "провенанс_звеньев": [l.get("провенанс") if l else "звено без данных (П8)" for l in path],
            "path_edges": path_edges,                 # рёбра пути для форвард-атрибуции (forward_promotion)
        })
    out.sort(key=lambda o: o.get("edge_rank") or 0.0, reverse=True)
    return {"chain_id": chain.get("id"), "shock": shock0, "horizon_days": horizon_days, "узлы": out}


# ── боевая обвязка: источники данных из storage/oracle.db ────────────────────────────
def build_from_db(chain, shock0, *, horizon_days, con, db=None, promotions=None):
    """build_chain_nodes с боевыми доступами к quotes (чувствительность на лету, отыгранное, вола).
    promotions=None → грузим knowledge/forward_promotions.yaml (форвард-заработанные ярус-A рёбра)."""
    if promotions is None:
        promotions = load_promotions()
    def has_data_fn(sym):
        n = con.execute("SELECT COUNT(*) FROM quotes WHERE symbol=? AND close IS NOT NULL",
                        (sym,)).fetchone()[0]
        return n >= MIN_BARS

    def _closes(sym, limit):
        # F0#8: adjusted_close — realized/вола на сыром close искажаются корпдействиями (edge корёжится)
        rows = con.execute("SELECT COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
                           "AND COALESCE(adjusted_close, close) IS NOT NULL "
                           "ORDER BY date DESC LIMIT ?", (sym, limit)).fetchall()
        return [float(r[0]) for r in reversed(rows)]

    def realized_fn(sym, h):
        # §R2.1: реализованное = реакция терминала за ОКНО СОБЫТИЯ (не за горизонт) — выровнено с
        # шоком корня, который тоже меряется за это окно. Иначе несоосность горизонтов (прошлый баг).
        return CAS.window_return(_closes(sym, CAS.EVENT_WINDOW_DAYS + 1))

    def vol_fn(sym):
        cl = _closes(sym, 61)
        r = CAS.log_returns(cl)
        return float(r.std()) if r.size >= 2 else 0.0

    def sensitivity_fn(up, down, lag):
        return SEN.on_the_fly(up, down, lag=lag, db=db)

    return build_chain_nodes(chain, shock0, horizon_days=horizon_days,
                             sensitivity_fn=sensitivity_fn, realized_fn=realized_fn,
                             vol_fn=vol_fn, has_data_fn=has_data_fn, promotions=promotions)
