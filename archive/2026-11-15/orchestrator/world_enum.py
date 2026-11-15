# -*- coding: utf-8 -*-
"""orchestrator/world_enum.py — Э4 «Перебор мира»: общий конвейер каркаса (а)+(б)+(д)+(ж).

Программа «Поисковый движок» (spec/ROADMAP_2026-07_search_engine.md, этап Э4, подпись 13.07):
событие → LLM-карта сегментов (world_map, а) → детерминированный скрин сегмент→инструменты
(segment_screen, б) → ВОЗВРАТ ПО ПРИЧИНЕ (д): отказ кандидата классифицируется КОДОМ —
  • ИНСТРУМЕНТ-специфичный (нет истории / нет ликвидности / не sealable / нет данных скрина)
    → следующий по списку; кэп попыток на событие + бюджет события (config/limits.yaml
    world_enum.*, зафиксированы ДО прогонов — рамка 3);
  • СОБЫТИЕ-специфичный (карта пуста / шок не подтверждён) → стоп по событию с журналом причины.
Каждый возврат — строка протокола {категория, кандидат, причина дословно} → «отсев_по_критериям»
восстановим (закрывает дыру логирования SYNC §4.2).

(ж) Кормление библиотеки B4 (решение владельца 13.07 №7 — АВТО-append): каждая пара
«источник→инструмент» с механизмом из карты — кандидат-ребро в append-only реестр
knowledge/edge_candidates.jsonl с провенансом (событие, сегмент, механизм, дата); загрузчик
edge_forward подхватывает их в ежедневный форвард-тест (origin=world_enum), дедуп против
библиотеки. Промоушен — по-прежнему ТОЛЬКО рукой владельца (promote_edges --apply).

(в)(г) Скоринг условным движком и ранг — ЗАГЛУШКИ: гейтуются этапом Д3 (условная амплитуда),
см. score_pair_conditional()/rank_pair() — честный NotImplementedError, research-метка.

РАМКИ: боевой контур event_first НЕ переключается (интеграция — Э5); боевые журналы
predictions/outcomes НЕ трогаются (здесь ни одного seal); LLM-величины запрещены (числа — код).
"""
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import world_map as WM               # noqa: E402
from orchestrator import segment_screen as SS          # noqa: E402
from orchestrator import universe_resolver as U        # noqa: E402
from orchestrator import run_budget as RB              # noqa: E402
from orchestrator import progress as PROG              # noqa: E402

DB = ROOT / "storage" / "oracle.db"
CANDIDATES = ROOT / "knowledge" / "edge_candidates.jsonl"     # (ж) append-only реестр кандидат-рёбер
MAPS_REGISTRY = ROOT / "journal" / "world_maps.jsonl"         # (е) реестр карт для пере-скрина
LOGS = ROOT / "journal" / "world_enum_logs"

# Лаг кандидат-ребра: карта мира лагов НЕ даёт (LLM-величины запрещены, рамка 2) → 0, как у всей
# эмпирической библиотеки (knowledge/causal_links: дневные лаги = 0); честный лаг ребро заработает
# форвард-статистикой B4, не назначением.
CANDIDATE_LAG_DAYS = 0


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def enum_config(limits=None):
    """Константы конвейера из config/limits.yaml world_enum (зафиксированы ДО прогонов, рамка 3)."""
    if limits is None:
        from mathlib import limits as L
        limits = L.load_limits()
    w = (limits or {}).get("world_enum") or {}
    return {
        "max_attempts_per_event": int(w.get("max_attempts_per_event", 400)),
        "target_instruments_min": int(w.get("target_instruments_min", 100)),
        "target_instruments_max": int(w.get("target_instruments_max", 300)),
        "map_ttl_days": int(w.get("map_ttl_days", 28)),
        "world_map_cap_usd": float(((limits or {}).get("per_run_token_budget_usd") or {})
                                   .get("world_map", 3.0)),
    }


# ── (в)(г) — ЗАГЛУШКИ до Д3 ──────────────────────────────────────────────────────
def score_pair_conditional(pair, **_kw):
    """Э4(в): скоринг пары условным движком (sensitivity_conditional + BH-поправка ВНУТРИ события).
    ГЕЙТУЕТСЯ этапом Д3 (тег se-d3): условного оценивателя ещё нет — честный отказ, не суррогат
    (П8: безусловная бета сюда НЕ подставляется, это ровно та ошибка, которую чинит Д3)."""
    raise NotImplementedError(
        "Э4(в) скоринг пар: ждёт этапа Д3 (условная амплитуда, walk-forward) — "
        "spec/ROADMAP_2026-07_search_engine.md; research-only, чисел нет (П8)")


def rank_pair(pair, **_kw):
    """Э4(г): ранг пары = условная чувствительность × неотыгранность × бонус порядка 2–4.
    ГЕЙТУЕТСЯ этапом Д3 (входное измерение — из (в))."""
    raise NotImplementedError(
        "Э4(г) ранг пар: ждёт этапа Д3 (и Э4(в)) — spec/ROADMAP_2026-07_search_engine.md; "
        "research-only, чисел нет (П8)")


# ── (ж) реестр кандидат-рёбер ────────────────────────────────────────────────────
def read_edge_candidates(path=None):
    """Читает append-only реестр кандидат-рёбер. Битые строки пропускаются с подсчётом (П8)."""
    p = pathlib.Path(path) if path else CANDIDATES
    recs, broken = [], 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                broken += 1
    return recs, broken


def append_edge_candidates(pairs, *, path=None, sens_path=None, now_dt=None, run_id=None):
    """(ж) Кандидат-рёбра «источник→инструмент» с механизмом → append в реестр с провенансом.

    Дедуп: против существующей БИБЛИОТЕКИ рёбер (edge_forward.edge_library по sens_path) и против
    уже записанных кандидатов (по edge_key). Возвращает {added, dup_library, dup_candidates, рёбра}."""
    from orchestrator import edge_forward as EFW
    from mathlib.calibration import forward_promotion as FP
    p = pathlib.Path(path) if path else CANDIDATES
    lib_keys = {FP.edge_key(e["from"], e["to"], e["lag"])
                for e in EFW.edge_library(sens_path or EFW.SENS, candidates_path=None)}
    existing, _ = read_edge_candidates(p)
    cand_keys = {r.get("edge_key") for r in existing}
    now_iso = (now_dt or _now()).isoformat(timespec="seconds")
    added, dup_lib, dup_cand, keys = [], 0, 0, []
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for pr in pairs:
            key = FP.edge_key(pr["источник"], pr["инструмент"], CANDIDATE_LAG_DAYS)
            if key in lib_keys:
                dup_lib += 1
                continue
            if key in cand_keys:
                dup_cand += 1
                continue
            rec = {"ts": now_iso, "edge_key": key, "origin": "world_enum",
                   "from": pr["источник"], "to": pr["инструмент"], "lag": CANDIDATE_LAG_DAYS,
                   "событие": pr.get("событие"), "сегмент": pr.get("сегмент"),
                   "порядок": pr.get("порядок"), "механизм": pr.get("механизм"),
                   "run_id": run_id,
                   "провенанс": "Э4(ж) авто-append (решение владельца 13.07 №7); лаг 0 — "
                                "не измерен, зарабатывается форвард-тестом B4"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cand_keys.add(key)
            added.append(key)
            keys.append(key)
    return {"added": len(added), "dup_library": dup_lib, "dup_candidates": dup_cand, "рёбра": keys}


# ── (е) реестр карт (для еженедельного пере-скрина ops/rescan_maps.py) ───────────
def register_map(event, карта, ttl_days, instruments, *, run_id, path=None, now_dt=None):
    """Append карты в реестр journal/world_maps.jsonl: срок жизни — поле карты (ставит КОД)."""
    p = pathlib.Path(path) if path else MAPS_REGISTRY
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": (now_dt or _now()).isoformat(timespec="seconds"), "run_id": run_id,
           "событие": event.get("событие"), "источник_шока": event.get("источник_шока"),
           "ttl_days": int(ttl_days), "карта": карта,
           "инструменты": sorted(instruments)}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def active_maps(path=None, now_dt=None):
    """Активные карты реестра: ts + ttl_days не истёк. Последняя запись события побеждает."""
    recs, _ = read_edge_candidates(path or MAPS_REGISTRY)   # тот же jsonl-ридер
    now = (now_dt or _now())
    by_event = {}
    for r in recs:
        try:
            ts = datetime.datetime.fromisoformat(str(r.get("ts")))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        if (now - ts).days <= int(r.get("ttl_days") or 0):
            by_event[r.get("событие")] = r
    return list(by_event.values())


# ── главный конвейер события ─────────────────────────────────────────────────────
def enumerate_event(event, *, client=None, map_doc=None, api_key=None, con=None, db=None,
                    fetch=None, universe=None, limits=None, allow_history_fetch=False,
                    write_candidates=False, candidates_path=None, sens_path=None,
                    write_registry=False, registry_path=None,
                    notices_path=None, write=False, now_dt=None):
    """Полный проход по ОДНОМУ событию: карта → скрин → классифицированный отсев → пары.

    event: {"событие", "ключи", "источник_шока", "shock", "дата"}.
    map_doc — готовый конверт карты (фикстура/реестр) ВМЕСТО live-вызова world_map (разработка/
    mock-гейт: LIVE LLM в разработке запрещён). client нужен только когда map_doc не задан.
    allow_history_fetch=False в каркасе: боевая БД в разработке read-only, добор истории — Э5
    (решение №5 остаётся в силе: в бою добор без потолка + квотный алерт, см. segment_screen).

    Возвращает протокол с attrition-таблицей: возвраты (д) с категориями, бюджет события ВИДЕН.
    """
    now_dt = now_dt or _now()
    cfg = enum_config(limits)
    run_id = "we_" + now_dt.strftime("%Y%m%dT%H%M%SZ")
    возвраты, отсев = [], {}

    def _reject(категория, кандидат, причина):
        возвраты.append({"категория": категория, "кандидат": кандидат, "причина": причина})
        отсев[причина.split(":")[0]] = отсев.get(причина.split(":")[0], 0) + 1

    # ── бюджет события: суб-потолок world_map (решение №8) — гард ставится ДО вызова карты
    guard = RB.RunBudgetGuard("world_map", cfg["world_map_cap_usd"])
    protocol = {
        "run_id": run_id, "ts": now_dt.isoformat(timespec="seconds"), "mode": "mock",
        "режим": "Э4 перебор мира — КАРКАС (боевой контур не переключён, Э5)",
        "событие": {k: event.get(k) for k in ("событие", "ключи", "источник_шока", "shock", "дата")},
        "кэп_попыток": cfg["max_attempts_per_event"],
        "бюджет_события": {"cap_usd": cfg["world_map_cap_usd"], "spent_usd": 0.0, "вызовов_llm": 0},
        "скоринг_ранг": "ЗАГЛУШКА: Э4(в)(г) гейтуются этапом Д3 (условная амплитуда) — "
                        "score_pair_conditional/rank_pair бросают NotImplementedError (research)",
        "spec_ref": "spec/ROADMAP_2026-07_search_engine.md Э4(а)(б)(д)(ж); рамки программы 1–4",
    }

    # ── СОБЫТИЕ-специфичные стопы (д): шок не подтверждён / карта пуста
    if event.get("shock") is None:
        protocol.update({"стоп_события": "шок источника не подтверждён (нет данных окна шока, П8)",
                         "перечислено_инструментов": 0, "пары": [], "возвраты": возвраты,
                         "отсев_по_критериям": отсев})
        return _finish(protocol, write)

    if map_doc is not None:
        envelope = dict(map_doc)
        envelope.setdefault("ttl_days", cfg["map_ttl_days"])
        envelope.setdefault("провенанс", {"источник": "фикстура/реестр (без LLM)"})
    else:
        if client is None:
            raise ValueError("нужен client (MockClient в разработке) либо map_doc-фикстура")
        prev_guard = getattr(client, "cost_guard", None)
        client.cost_guard = guard
        try:
            envelope = WM.build_world_map(event, client, run_id=run_id, limits=limits)
        finally:
            client.cost_guard = prev_guard
    protocol["бюджет_события"].update({"spent_usd": round(guard.spent_usd, 4),
                                       "вызовов_llm": guard.calls})
    protocol["карта_провенанс"] = envelope.get("провенанс")
    protocol["карта_проблемы_валидации"] = envelope.get("проблемы_валидации") or []
    карта = envelope.get("карта")
    if not карта or not карта.get("сегменты"):
        protocol.update({"стоп_события": "карта пуста/отказ: " + str(envelope.get("отказ")),
                         "перечислено_инструментов": 0, "пары": [], "возвраты": возвраты,
                         "отсев_по_критериям": отсев})
        return _finish(protocol, write)
    protocol["карта"] = карта
    protocol["ttl_days"] = envelope.get("ttl_days", cfg["map_ttl_days"])

    # ── скрин по сегментам + возврат по причине (д)
    own = con is None
    if con is None:
        con = sqlite3.connect(str(db or DB), timeout=30)
    источник = event.get("источник_шока")
    попыток, кэп_достигнут = 0, False
    seen_syms, перечислено, пары = set(), [], []
    сегменты_протокол = []
    target_max = cfg["target_instruments_max"]
    target_min = cfg["target_instruments_min"]
    n_seg = len(карта["сегменты"])
    отсечённые_квотой = []           # сегменты, где квота срезала (было больше) — кандидаты 2-го прохода

    def _process(seg, квота):
        """Скрин одного сегмента с квотой + классифицированный отсев (д). Возвращает
        (n_доступно, взято) — сколько скрин вернул и сколько НОВЫХ инструментов перечислено."""
        nonlocal попыток, кэп_достигнут
        остаток_л = target_max - len(перечислено)
        scr = SS.screen_segment(seg, api_key=api_key, con=con, universe=universe,
                                max_instruments=min(квота, остаток_л), fetch=fetch,
                                notices_path=notices_path)
        сегменты_протокол.append({"сегмент": seg["сегмент"], "порядок": seg["порядок"],
                                  "источник_скрина": scr["источник"], "квота": квота,
                                  "инструментов": len(scr["инструменты"])})
        if not scr["инструменты"]:
            _reject("инструмент", f"сегмент:{seg['сегмент']}",
                    "нет данных скрина: " + str(scr.get("отказ") or "скрин вернул пусто"))
            return 0, 0
        rows = SS.annotate_sealable(scr["инструменты"], con=con)
        if allow_history_fetch and api_key is not None:
            missing = [r["symbol"] for r in rows if not r["sealable"]]
            if missing:
                SS.backfill_history(missing, api_key, con=con, notices_path=notices_path)
                rows = SS.annotate_sealable(rows, con=con)
        взято = 0
        for r in rows:
            sym = r["symbol"]
            if sym in seen_syms:
                continue              # дубль между сегментами/проходами — не попытка и не возврат
            if попыток >= cfg["max_attempts_per_event"]:
                кэп_достигнут = True
                _reject("инструмент", sym,
                        f"кэп попыток на событие: {cfg['max_attempts_per_event']} (config world_enum)")
                break
            попыток += 1
            seen_syms.add(sym)
            перечислено.append({**r, "сегмент": seg["сегмент"], "порядок": seg["порядок"]})
            взято += 1
            if sym == источник:
                _reject("инструмент", sym, "совпадает с источником шока — самопетля")
                continue
            if not r["sealable"]:
                _reject("инструмент", sym,
                        "нет истории/не sealable: <" + str(U.MIN_SEALABLE_BARS) +
                        " баров в quotes (§9/П16)" +
                        ("" if allow_history_fetch else "; добор истории в каркасе отключён "
                         "(боевая БД read-only до Э5; в бою — без потолка, решение №5)"))
                continue
            пары.append({"источник": источник, "инструмент": sym,
                         "событие": event.get("событие"),
                         "сегмент": seg["сегмент"], "порядок": seg["порядок"],
                         "направление_сегмента": seg["направление"],
                         "канал": seg.get("канал"), "механизм": seg["механизм"]})
        return len(scr["инструменты"]), взято

    try:
        # ── 1-й проход: ДИНАМИЧЕСКАЯ квота — на сегменте i квота = ceil(остаток / оставшиеся сегменты).
        # Так недобор широкого сегмента реально перетекает следующим (узкие сегменты 2–4 порядка
        # получают больше), а не режется статичным ceil(max/n). Комментарий = код (Э4-ревью medium).
        for i, seg in enumerate(карта["сегменты"]):
            if кэп_достигнут:
                break
            остаток = target_max - len(перечислено)
            if остаток <= 0:
                break
            осталось_сегментов = n_seg - i
            квота = max(1, -(-остаток // осталось_сегментов))          # ceil(остаток/осталось)
            n_доступно, _ = _process(seg, квота)
            if n_доступно >= квота and n_доступно >= 1:                 # квота срезала — есть ещё
                отсечённые_квотой.append(seg)
        # ── 2-й проход: если недобрали target_min и есть сегменты, срезанные квотой в 1-м проходе,
        # добираем из них до target_max (порядок карты; дубли гасит seen_syms). Э4-ревью (medium).
        if not кэп_достигнут and len(перечислено) < target_min and отсечённые_квотой:
            for seg in отсечённые_квотой:
                if кэп_достигнут:
                    break
                остаток = target_max - len(перечислено)
                if остаток <= 0:
                    break
                _process(seg, остаток)
    finally:
        if own:
            con.close()

    protocol.update({
        "сегменты_скрин": сегменты_протокол,
        "перечислено_инструментов": len(перечислено),
        "цель_инструментов": f"{cfg['target_instruments_min']}–{cfg['target_instruments_max']}",
        "попыток": попыток, "кэп_достигнут": кэп_достигнут,
        "принято_пар": len(пары),
        "пары": пары,
        "возвраты": возвраты,
        "отсев_по_критериям": отсев,
    })

    # ── (ж) авто-append кандидат-рёбер (решение №7); дедуп внутри append_edge_candidates
    if write_candidates and пары:
        protocol["кандидат_рёбра"] = append_edge_candidates(
            пары, path=candidates_path, sens_path=sens_path, now_dt=now_dt, run_id=run_id)
    if write_registry and карта:
        register_map(event, карта, protocol.get("ttl_days", cfg["map_ttl_days"]),
                     [p["инструмент"] for p in пары], run_id=run_id,
                     path=registry_path, now_dt=now_dt)
    return _finish(protocol, write)


def _finish(protocol, write):
    if write:
        LOGS.mkdir(parents=True, exist_ok=True)
        PROG.atomic_write_text(LOGS / f"{protocol['run_id']}.json",
                               json.dumps(protocol, ensure_ascii=False, indent=2))
    return protocol
