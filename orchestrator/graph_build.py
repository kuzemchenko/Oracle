# -*- coding: utf-8 -*-
"""orchestrator/graph_build.py — ГРАФ-СТРОИТЕЛЬ: событие → граф последствий → факты ворот → воронка
(REVISION_2026-06_cascade_graph_behavioral_loop.md §R2, Этап B2).

Связывает уже готовое:
  • cascade_build.build_from_db — из активированных авторских цепочек строит узлы-компании
    (амплитуда = НЕпрокинутый edge, ярусы A/B/C, надёжность Π, sealable_path) по боевым рядам;
  • universe_resolver.is_sealable — ворота торгуемости (§9-инструмент с источником цены и историей);
  • quotes — объём (ликвидность) и волатильность (шумовой пол σ для сигнал-над-шумом);
  • mathlib.cascade.node_sensitivity — изоляция: R² терминала НА КОРЕНЬ (доля дисперсии от каскада);
  • mathlib.graph_select.select — воронка B1 (жёсткие ворота → дешёвый пред-ранг → топ-K).

Инвариант 6: считает КОД. Узел без данных/инструмента НЕ выдумывается — честно отсеивается воротами (П8).
"""
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import cascade as CAS               # noqa: E402
from mathlib import graph_select as GS           # noqa: E402
from orchestrator import cascade_build as CB     # noqa: E402
from orchestrator import universe_resolver as U  # noqa: E402

ADV_BARS = 20            # окно для среднего объёма (ликвидность)
ISO_LOOKBACK = 400       # окно для изоляции/волы терминала


def _closes(con, sym, limit):
    rows = con.execute("SELECT close FROM quotes WHERE symbol=? AND close IS NOT NULL "
                       "ORDER BY date DESC LIMIT ?", (sym, limit)).fetchall()
    return [float(r[0]) for r in reversed(rows)]


def _adv(con, sym, n=ADV_BARS):
    """Средний дневной объём (последние n баров). None — нет данных объёма (→ провал ликвидности, П8)."""
    rows = con.execute("SELECT volume FROM quotes WHERE symbol=? AND volume IS NOT NULL "
                       "ORDER BY date DESC LIMIT ?", (sym, n)).fetchall()
    vals = [float(r[0]) for r in rows if r[0] is not None]
    return sum(vals) / len(vals) if vals else None


def _isolation_r2_and_sigma(con, root, term, lookback=ISO_LOOKBACK):
    """Изоляция = R² терминала НА КОРЕНЬ (доля дисперсии инструмента, объяснённая шоком каскада) +
    шумовой пол σ (std дневных доходностей терминала). R² None при недостатке синхронной истории
    (§R2.1 → isolation_factor подставит структурный дефолт, а не выдумает число, П8)."""
    tc = CAS.log_returns(_closes(con, term, lookback))
    sigma = float(tc.std()) if tc.size >= 2 else None
    rc = CAS.log_returns(_closes(con, root, lookback))
    sens = CAS.node_sensitivity(rc, tc, lag=0)        # None если < MIN_OBS синхронных наблюдений
    r2 = sens["r2"] if sens else None
    return r2, sigma


def _min_reliability(compose_r, iso_r):
    """Консервативная надёжность связи = min из доступных оценок (compose по звеньям, изоляция). None
    только если обе None (П8: нет данных). Один оценщик вместо двух расходящихся (P1#6)."""
    vals = [x for x in (compose_r, iso_r) if isinstance(x, (int, float))]
    return min(vals) if vals else None


def node_to_facts(node, *, con, horizon_days, root_symbol=None):
    """Узел cascade_build → факты для ворот/пред-ранга B1 (graph_select). Резолвит торгуемость,
    объём, изоляцию-R², шумовой пол. Разрешимость = есть §9-инструмент И посчитана амплитуда (edge).
    Корень изоляции — node['root'] (своя у каждой цепочки в объединённом графе) ИЛИ фолбэк root_symbol."""
    sym = node.get("узел")
    root = node.get("root") or root_symbol
    r2, sigma = _isolation_r2_and_sigma(con, root, sym)
    sigma_h = sigma * math.sqrt(max(int(horizon_days), 1)) if sigma else None
    amp = node.get("amplitude")                       # НЕпрокинутый edge — рангуем по нему
    sealable = U.is_sealable(sym, con=con)
    return {
        "symbol": sym,
        "sealable": sealable,
        "adv": _adv(con, sym),
        "lag_days": node.get("lag_total"),
        "resolvable": bool(sealable and amp is not None),
        "tiers": node.get("tiers"),
        "amplitude": amp,
        "sigma_h": sigma_h,
        # P1#6: ЕДИНЫЙ честный оценчик надёжности = МИНИМУМ из двух (compose по звеньям vs прямая
        # изоляция терминал~корень). compose бывает оптимистичен; берём консервативный (П8).
        "reliability": _min_reliability(node.get("reliability_r2"), r2),
        "r2": r2,
        "probability": node.get("probability"),       # направленная P (для seal-спеки B3c)
        "horizon_days": int(horizon_days),
        # справочно для лога / дорогой ступени:
        "order": node.get("order"), "chokepoint": node.get("chokepoint"),
        "edge_total": node.get("amplitude_total"), "research": node.get("research"),
        # связь идеи с породившим её событием/цепочкой (для разбора в дайджесте: «почему»).
        # Прокидываем как есть — фабрикации нет, просто не теряем то, что уже посчитано.
        "_chain": node.get("_chain"), "root": node.get("root"),
        "path_edges": node.get("path_edges"),          # рёбра пути → форвард-атрибуция (forward_promotion)
    }


def select_from_nodes(raw_nodes, *, con, horizon_days, root_symbol=None, top_k=8):
    """Узлы cascade_build → факты → воронка B1. Возвращает результат graph_select.select.
    root_symbol — фолбэк корня изоляции; в объединённом графе каждый узел несёт свой node['root']."""
    facts = [node_to_facts(n, con=con, root_symbol=root_symbol, horizon_days=horizon_days)
             for n in (raw_nodes or [])]
    return GS.select(facts, top_k=top_k)


def route_tracks(selection):
    """Маршрутизация результата воронки по политике seal (REVISION §R3 «Два трека», решение 20.06):
      • money       — выжившие с ярус-A основой (node['research']==False): денежный трек §11;
      • provisional — выжившие ярус-B/C (node['research']==True): провизорный форвард-трек
                      (запечатан П16, отдельный Brier, к §11 НЕ приближается, выпуск по N исходам);
      • digest_only — отсеянные воротами (нет инструмента/ликвидности/окна/разрешимости): в
                      research-дайджест как контекст, НЕ запечатываются (прогноз не сформировать).

    Здесь только КЛАССИФИКАЦИЯ — само запечатывание/журнал отдельно (live + §11), герметично по треку.
    """
    money, provisional = [], []
    for s in selection.get("ранжировано", []):
        (provisional if (s.get("node") or {}).get("research") else money).append(s)
    return {"money": money, "provisional": provisional,
            "digest_only": selection.get("отсев", [])}


def build_graph(shock_source, shock_move, *, con, db=None, horizon_days=20, top_k=8, chains=None):
    """Событие (шок источника) → граф последствий из активированных цепочек → воронка отбора.

    chains=None → авторские цепочки, чей якорь = источник (cascade_build.chains_for_source). Узлы
    ВСЕХ активированных цепочек объединяются в один граф (коллизии по инструменту видит graph_select).
    """
    chains = CB.chains_for_source(shock_source) if chains is None else chains
    raw_nodes, used = [], []
    for ch in chains:
        res = CB.build_from_db(ch, shock_move, horizon_days=horizon_days, con=con, db=db)
        used.append(res.get("chain_id"))
        raw_nodes.extend(res.get("узлы") or [])
    sel = select_from_nodes(raw_nodes, con=con, root_symbol=shock_source,
                            horizon_days=horizon_days, top_k=top_k)
    return {"источник": shock_source, "shock": shock_move, "horizon_days": horizon_days,
            "цепочки": used, "граф_узлов": len(raw_nodes), "отбор": sel}
