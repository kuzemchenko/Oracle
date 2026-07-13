# -*- coding: utf-8 -*-
"""ops/rescan_maps.py — Э4(е) «Перебор мира»: ЕЖЕНЕДЕЛЬНЫЙ пере-скрин активных карт мира.

Программа «Поисковый движок» (spec/ROADMAP_2026-07_search_engine.md): карта тектонического
события живёт дольше дня (срок жизни ttl_days — поле карты, ставит код). Раз в неделю по
активным картам реестра journal/world_maps.jsonl пере-прогоняется детерминированный скрин
Э4(б) (новые инструменты, изменившаяся ликвидность) — ДИФФ в протокол.

Расписание — ВОСКРЕСЕНЬЕ 09:00 (решение владельца 13.07, Вопрос 6), после воскресного блока
08:00/08:30; БЕЗ сообщений владельцу (только протокол; алерт — лишь квота EODHD через
notices-канал внутри segment_screen).

Только детерминированный код (Инв#6), LLM нет. Боевые журналы прогнозов не трогаются.
"""
import argparse
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import world_enum as WE          # noqa: E402
from orchestrator import segment_screen as SS      # noqa: E402
from orchestrator import progress as PROG          # noqa: E402

DB = ROOT / "storage" / "oracle.db"
LOGS = ROOT / "journal" / "world_enum_logs"


def rescan(*, registry_path=None, api_key=None, con=None, db=None, universe=None,
           fetch=None, notices_path=None, write=True, now_dt=None):
    """Пере-скрин всех активных карт. Возвращает протокол с диффом по каждой карте."""
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    run_id = "rescan_" + now_dt.strftime("%Y%m%dT%H%M%SZ")
    maps = WE.active_maps(registry_path, now_dt=now_dt)
    cfg = WE.enum_config()
    own = con is None
    if con is None:
        con = sqlite3.connect(str(db or DB), timeout=30)
    target_max = cfg["target_instruments_max"]
    карты = []
    try:
        for m in maps:
            прежние = set(m.get("инструменты") or [])
            источник = m.get("источник_шока")             # исключаем источник — как enumerate_event (д)
            segs = (m.get("карта") or {}).get("сегменты") or []
            n_seg = len(segs)
            текущие, seen, сегменты = set(), set(), []
            # ВОСПРОИЗВОДИМ правила enumerate_event (Э4-ревью medium): та же ДИНАМИЧЕСКАЯ квота на
            # сегмент (ceil(остаток/оставшиеся)) и исключение источника шока — иначе дифф несравним
            # (раньше был кэп 300/сегмент без квоты и источник попадал в текущие).
            for i, seg in enumerate(segs):
                остаток = target_max - len(seen)
                if остаток <= 0:
                    break
                квота = max(1, -(-остаток // (n_seg - i)))          # ceil(остаток/оставшиеся)
                scr = SS.screen_segment(seg, api_key=api_key, con=con, universe=universe,
                                        max_instruments=min(квота, остаток),
                                        fetch=fetch, notices_path=notices_path)
                rows = SS.annotate_sealable(scr["инструменты"], con=con)
                ok = set()
                for r in rows:
                    sym = r["symbol"]
                    if sym in seen or sym == источник:           # дедуп + исключение источника
                        continue
                    seen.add(sym)
                    if r.get("sealable"):
                        ok.add(sym)
                текущие |= ok
                сегменты.append({"сегмент": seg.get("сегмент"),
                                 "источник_скрина": scr["источник"], "квота": квота,
                                 "инструментов": len(rows), "sealable": len(ok)})
            карты.append({
                "событие": m.get("событие"), "ts_карты": m.get("ts"),
                "ttl_days": m.get("ttl_days"),
                "сегменты": сегменты,
                "дифф": {"было": len(прежние), "стало": len(текущие),
                         "добавились": sorted(текущие - прежние),
                         "выпали": sorted(прежние - текущие)},
            })
    finally:
        if own:
            con.close()
    protocol = {"run_id": run_id, "ts": now_dt.isoformat(timespec="seconds"), "mode": "mock",
                "режим": "Э4(е) еженедельный пере-скрин активных карт мира",
                "активных_карт": len(maps), "карты": карты,
                "spec_ref": "spec/ROADMAP_2026-07_search_engine.md Э4(е); решение владельца №6"}
    if write:
        LOGS.mkdir(parents=True, exist_ok=True)
        PROG.atomic_write_text(LOGS / f"{run_id}.json",
                               json.dumps(protocol, ensure_ascii=False, indent=2))
    return protocol


def main(argv=None):
    import os
    ap = argparse.ArgumentParser(description="Э4(е): еженедельный пере-скрин активных карт мира")
    ap.add_argument("--no-api", action="store_true", help="без EODHD screener (фолбэк БД)")
    args = ap.parse_args(argv)
    api_key = None if args.no_api else os.environ.get("EODHD_API_KEY")
    p = rescan(api_key=api_key)
    print(f"[{p['run_id']}] активных карт: {p['активных_карт']}")
    for k in p["карты"]:
        d = k["дифф"]
        print(f"  {str(k['событие'])[:60]}: {d['было']}→{d['стало']} "
              f"(+{len(d['добавились'])}/−{len(d['выпали'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
