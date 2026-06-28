# -*- coding: utf-8 -*-
"""mathlib/graph_select.py — ДЕТЕРМИНИРОВАННЫЙ отбор узлов графа последствий
(REVISION_2026-06_cascade_graph_behavioral_loop.md §R2, Этап B1).

Граф от одного события — до ~100 узлов 2–4 порядка. Отбор — ВОРОНКА, не плоский чек-лист
(порядок критичен: дёшево-убивающее раньше дорогого):
  Ступень 0 — ЖЁСТКИЕ ВОРОТА (бинарно, по всем узлам). Провал → research/watchlist, дорогую
              математику и LLM не тратим.
  Ступень 1 — ДЕШЁВЫЙ ПРЕД-РАНГ (мультипликативно). Сортировка → топ-K в дорогой анализ.

ВОРОТА — ТОЛЬКО про пригодность к ДЕЙСТВИЮ: торгуемость, ликвидность, инвестируемое окно,
§9-разрешимость. Нехватка истории/эмпирики (ярус C) НЕ отбрасывает событие (директива 20.06):
иначе вылетали бы именно неочевидные дальние узлы — суть продукта. Ярус снижает РАНГ (через
надёжность в prerank) и определяет политику seal (провизорный vs денежный), но узел ОСТАЁТСЯ в потоке.

Инвариант 6 (CLAUDE.md): считает КОД, не LLM. Модуль ЧИСТЫЙ — на вход ФАКТЫ по узлу (их
резолвит оркестратор B2/B3: is_sealable из universe_resolver, объём из quotes, бета/R²/амплитуда
из mathlib.cascade, §9-разрешимость из forecast), на выход вердикт+балл. Ни БД, ни сети, ни LLM.

Центральное напряжение (§R2 + директива 20.06): discovery-ранг = ВОЗМОЖНОСТЬ = неотыгранный
edge через snr × изоляция. НАДЁЖНОСТЬ в ранг НЕ множится — иначе ярус-C «не знаю»^хопы
(0.2³≈0.008) хоронит ровно дальние неочевидные узлы (= ловушка Brent с другой стороны: наверх
лезет только давно установленное и уже отыгранное). Надёжность/ярус → МЕТКА уверенности +
ворота ДЕНЕЖНОГО трека (route_tracks), но discovery-ранг не давит. «Измеренность» связи входит
через изоляцию (R² терминала на корень); магнитуду неотыгранного несёт snr (|edge|/σ_h).
"""
import math

# ── пороги (оркестратор может переопределить под событие/режим) ──────────────────────
DEFAULT_MIN_ADV = 100_000     # мин. средний дневной объём инструмента (как verify_tickers, §14)
DEFAULT_MAX_WINDOW = 180      # инвестируемое окно лага переноса (торг. дни ≈ 9 мес); дальше → watchlist
STRUCTURAL_ISOLATION = 0.3    # изоляция, когда R² НЕИЗМЕРИМ (нет истории): консервативный дефолт —
                              # НЕ выдумка (§R2.1, П8); узел честно проседает, а не получает фикт. высокую
COLLISION_LAMBDA = 0.5        # сила штрафа за коллизию каскадов (§R2.4 — неоднозначно, см. collision_count)


def sigma_horizon(resid_std, horizon_days):
    """Остаточная вола на горизонте: σ_h = resid_std·√h (та же формула, что mathlib.cascade.node_probability)."""
    if resid_std is None:
        return None
    return float(resid_std) * math.sqrt(max(int(horizon_days), 1))


def signal_to_noise(amplitude, sigma_h):
    """Сигнал-над-шумом = |амплитуда| / σ_h. Прогноз движения обязан пробить шумовой пол
    инструмента, иначе он недетектируем и неторгуем. None — нет волы (σ_h None или 0)."""
    if amplitude is None or sigma_h is None or not (float(sigma_h) > 0):
        return None
    return round(abs(float(amplitude)) / float(sigma_h), 6)


def collision_count(symbol, all_targets):
    """Сколько ДРУГИХ активных каскадов целятся в тот же инструмент (§R2.4). Один инструмент от
    многих каскадов — подтверждение ИЛИ перегретый консенсус (неоднозначно). Различение
    независимости драйверов — задача дорогой ступени; здесь только счётчик + мягкий штраф."""
    s = str(symbol).upper()
    return max(sum(1 for t in all_targets if str(t).upper() == s) - 1, 0)


def isolation_factor(r2, *, collisions=0):
    """Изоляция сигнала ∈ [0,1]: какую долю движения инструмента объясняет ИМЕННО этот каскад,
    со штрафом за коллизии. Высокая изоляция = сигнал не утоплен другими драйверами.

    r2 None (нет истории измерить) → НЕ выдумываем: консервативный структурный дефолт с пометкой
    «неизмеримо» (§R2.1, П8). Так узел честно проседает в ранге, а не получает фиктивно высокую
    изоляцию из воздуха."""
    if r2 is None:
        base, prov = STRUCTURAL_ISOLATION, "R² неизмерим (нет истории) → структурный дефолт (П8)"
    else:
        base = max(0.0, min(float(r2), 1.0))
        prov = f"R²={round(float(r2), 4)} (доля дисперсии инструмента от каскада)"
    discount = 1.0 / (1.0 + COLLISION_LAMBDA * max(int(collisions), 0))
    return {"factor": round(base * discount, 6), "r2_base": round(base, 6),
            "collisions": int(collisions), "collision_discount": round(discount, 4),
            "провенанс": prov}


def gate_node(*, sealable, adv, lag_days, resolvable,
              min_adv=DEFAULT_MIN_ADV, max_window=DEFAULT_MAX_WINDOW):
    """Жёсткие ворота ступени 0 (§R2) — ТОЛЬКО про пригодность к ДЕЙСТВИЮ. Любой провал → узел в
    research/watchlist, дорогую математику не тратим. Возвращает {passed, fails:[(критерий, причина)]}.

    ВАЖНО (директива 20.06): нехватка истории/эмпирики (ярус C) НЕ отбрасывает событие — иначе
    вылетали бы именно неочевидные дальние узлы, ради которых всё затеяно. Ярус влияет на РАНГ
    (через надёжность в prerank) и на политику seal (провизорный vs денежный), но здесь НЕ гейтит.

    Входы — ФАКТЫ, резолвленные оркестратором (П8):
      sealable   — есть §9-разрешимый инструмент (universe_resolver.is_sealable);
      adv        — средний дневной объём инструмента (None = нет данных → провал ликвидности);
      lag_days   — лаг переноса узла (None = неизвестен → провал окна);
      resolvable — формулируется как §9-прогноз порог+дата (forecast.validate_resolvable).
    """
    fails = []
    if not sealable:
        fails.append(("торгуемость", "нет §9-разрешимого инструмента в узле"))
    if adv is None or float(adv) < float(min_adv):
        fails.append(("ликвидность", f"средний объём {adv} < порога {min_adv}"))
    if lag_days is None or int(lag_days) < 0 or int(lag_days) > int(max_window):
        fails.append(("окно", f"лаг {lag_days} вне инвестируемого окна [0,{max_window}] дн"))
    if not resolvable:
        fails.append(("разрешимость", "не формулируется как §9-прогноз (порог+дата)"))
    return {"passed": not fails, "fails": fails}


def prerank(*, amplitude, sigma_h, reliability, r2, collisions=0):
    """Дешёвый пред-ранг discovery (§R2, директива 20.06): score = ВОЗМОЖНОСТЬ = snr × изоляция.

    Надёжность НЕ множится в ранг. Иначе ярус-C «не знаю»^хопы (0.2³≈0.008) хоронит ровно дальние
    неочевидные узлы — суть продукта. reliability несётся как МЕТКА уверенности и питает ворота
    ДЕНЕЖНОГО трека (route_tracks), но discovery-ранг не давит. «Измеренность» связи и так входит
    через изоляцию (R² терминала на корень); snr несёт неотыгранную магнитуду (|edge|/σ_h).
    Любой около нуля из {snr, изоляция} убивает (нет волы → недетектируемо; нулевая изоляция → утоплен)."""
    snr = signal_to_noise(amplitude, sigma_h)
    iso = isolation_factor(r2, collisions=collisions)
    rel = None if reliability is None else round(max(0.0, float(reliability)), 4)
    if snr is None:
        return {"score": 0.0, "snr": None, "reliability": rel, "isolation": iso,
                "причина_ноль": "нет σ — движение недетектируемо (нет волы/данных, П8)"}
    return {"score": round(snr * iso["factor"], 6), "snr": snr,
            "reliability": rel, "isolation": iso, "причина_ноль": None}


def select(nodes, *, top_k=8, min_adv=DEFAULT_MIN_ADV, max_window=DEFAULT_MAX_WINDOW):
    """Прогон воронки по узлам графа: ворота → пред-ранг → топ-K (§R2, гейт §R8 — отсев виден
    по каждому критерию). Узел — dict с фактами: symbol, sealable, adv, lag_days, resolvable,
    tiers, amplitude, reliability, r2, и (sigma_h ЛИБО resid_std+horizon_days).

    Коллизии считаются по ВСЕМУ графу (символы всех узлов), не только выживших — так перегрев
    инструмента виден, даже если часть метящих в него каскадов отсеяна воротами."""
    nodes = list(nodes or [])
    targets = [n.get("symbol") for n in nodes]
    gated_out, survivors = [], []
    for n in nodes:
        g = gate_node(sealable=n.get("sealable"), adv=n.get("adv"), lag_days=n.get("lag_days"),
                      resolvable=n.get("resolvable"), min_adv=min_adv, max_window=max_window)
        if not g["passed"]:
            gated_out.append({"symbol": n.get("symbol"), "fails": g["fails"]})
            continue
        sh = n.get("sigma_h")
        if sh is None:
            sh = sigma_horizon(n.get("resid_std"), n.get("horizon_days", 1))
        coll = collision_count(n.get("symbol"), targets)
        pr = prerank(amplitude=n.get("amplitude"), sigma_h=sh,
                     reliability=n.get("reliability"), r2=n.get("r2"), collisions=coll)
        survivors.append({"symbol": n.get("symbol"), "score": pr["score"], "prerank": pr, "node": n})
    survivors.sort(key=lambda r: r["score"], reverse=True)

    # ДЕДУП ПО ТИКЕРУ (B3c): несколько каскадных путей в один инструмент → берём ЛУЧШИЙ ЯРУС
    # (money-путь, node.research==False, важнее провизорного), затем выше score. Иначе один тикер
    # запечатывается дважды (коррелированные дубли в журнале). Отброшенные — в лог (прозрачность П8).
    best, дедуп_отброшено = {}, []
    for s in survivors:
        sym = s["symbol"]
        rank = (not (s.get("node") or {}).get("research"), s["score"])   # money>провиз, потом score
        cur = best.get(sym)
        if cur is None or rank > cur[0]:
            if cur is not None:
                дедуп_отброшено.append(sym)
            best[sym] = (rank, s)
        else:
            дедуп_отброшено.append(sym)
    deduped = sorted((v[1] for v in best.values()), key=lambda r: r["score"], reverse=True)

    crit = {}
    for go in gated_out:
        for c, _ in go["fails"]:
            crit[c] = crit.get(c, 0) + 1
    return {
        "всего": len(nodes),
        "ворота_прошли": len(survivors),     # прошли ворота (до дедупа)
        "дедуп_отброшено": дедуп_отброшено,  # дубли тикера, схлопнутые к лучшему пути
        "отсев": gated_out,                  # по каждому критерию (П8-прозрачность)
        "отсев_по_критериям": crit,          # агрегат: сколько узлов завалил каждый критерий
        "ранжировано": deduped,
        "топ_k": deduped[:max(int(top_k), 0)],
    }
