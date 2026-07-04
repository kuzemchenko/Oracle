# -*- coding: utf-8 -*-
"""ops/bot_introspect.py — R9b: детерминированные ИНТРОСПЕКЦИИ для чата-Дирижёра.

Дирижёр отвечает ПО ФАКТУ с цитатой (П8), не по памяти: эти read-функции тянут реальные числа из
БД / журналов / протоколов прогонов. relevant_fact(вопрос) роутит вопрос к нужной функции; результат
конструктор кладёт Дирижёру в заземление, тот объясняет словами — но число/расчёт здесь, кодом.

Только ЧТЕНИЕ. Ничего не меняет (R9: чат = предлагать+читать). Нет данных → честная пометка (П8).
"""
import json
import pathlib
import re
import sqlite3
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOGS = ROOT / "journal" / "funnel_logs"
DB = ROOT / "storage" / "oracle.db"


def _con():
    return sqlite3.connect(str(DB), timeout=30) if DB.exists() else None


def _latest_ef():
    files = [f for f in sorted(LOGS.glob("ef_*.json")) if "__" not in f.name]
    return files[-1] if files else None


def _ticker(q):
    m = re.findall(r"\b([A-Z]{2,5})(?:\.US)?\b", q or "")
    return (m[0] + ".US") if m else None


def brier_tracks():
    """Brier по трекам + гейт-270 (resolve §10.10). Герметично: денежный отдельно от провизорного."""
    from orchestrator import resolve as R
    s = R.run_resolve(write=False)
    pv = s.get("провизорный_трек") or {}
    k = s.get("KILL_проверка") or {}
    pp = (k.get("checks") or {}).get("порог_применимости") or {}
    if k.get("kill"):
        kill_line = "🛑 KILL §11 СРАБОТАЛ (детерминир.): " + "; ".join(k.get("reasons") or [])
    elif pp.get("применимо"):
        kill_line = "KILL §11: чисто (порог достигнут; edge/бенчмарк §30 не измеряется — FГ)"
    else:
        kill_line = f"KILL §11: чисто (до порога {pp.get('порог')} разрешённых прогнозов — не применим)"
    return (f"Brier ДЕНЕЖНЫЙ={s.get('brier')} (всего сверено исходов: {s.get('всего_исходов')}); "
            f"ПРОВИЗОРНЫЙ={pv.get('brier')} ({pv.get('исходов')} исходов, к §11 НЕ идёт); "
            f"до денежных ворот Б→Д осталось {s.get('до_ворот_270')} разрешённых прогнозов; "
            f"{kill_line}. [resolve §10.10]")


def borrow(symbol):
    """borrow-прокси по тикеру (§R4): cost-to-borrow EODHD не отдаёт → из short-данных+опционов."""
    con = _con()
    if con is None:
        return "нет БД."
    try:
        from orchestrator import context as C
        f = C._fundamentals(con).get(symbol) or {}
    finally:
        con.close()
    bp = f.get("borrow_proxy") or {}
    if bp.get("score") is None:
        return f"borrow-прокси {symbol}: нет данных (П8). cost-to-borrow EODHD не продаёт."
    return (f"borrow-прокси {symbol}={bp['score']} ({bp.get('провенанс')}). "
            f"Голой ставки cost-to-borrow у EODHD нет — это прокси (§R4). [context._fundamentals]")


def latest_graph():
    """Сводка последнего боевого event-first прогона: граф, ворота, треки, запечатано, топ."""
    f = _latest_ef()
    if not f:
        return "боевых event-first прогонов пока нет."
    p = json.loads(f.read_text(encoding="utf-8"))
    g = p.get("граф_отбор") or {}
    tr, zp = g.get("треки") or {}, g.get("запечатано") or {}
    top = "; ".join(f"{n.get('актив')} edge {n.get('edge')} {''.join(n.get('ярусы') or [])}"
                    for n in (g.get("топ_k") or [])[:5]) or "—"
    return (f"Последний прогон {p.get('run_id')}: граф {g.get('узлов', 0)} узлов, ворота {g.get('ворота_прошли', 0)}, "
            f"треки money {tr.get('money', 0)}/провиз {tr.get('провизорный', 0)}, "
            f"запечатано money {zp.get('money', 0)}/провиз {zp.get('провизорный', 0)}. Топ: {top}. [{f.name}]")


def why_node(symbol):
    """Почему тикер в топе / отсеян / не встречался — из последнего прогона."""
    f = _latest_ef()
    if not f:
        return "прогонов нет."
    p = json.loads(f.read_text(encoding="utf-8"))
    g = p.get("граф_отбор") or {}
    for bucket in ("money_трек", "провизорный_трек", "топ_k"):
        for n in (g.get(bucket) or []):
            if n.get("актив") == symbol:
                return (f"{symbol} в прогоне {p.get('run_id')}: score {n.get('score')}, edge {n.get('edge')}, "
                        f"ярусы {n.get('ярусы')}, лаг {n.get('лаг_дней')}д, надёжность {n.get('надёжность_метка')}, "
                        f"трек {'money' if bucket == 'money_трек' else 'провиз' if bucket == 'провизорный_трек' else '—'}. [{f.name}]")
    for go in (g.get("отсев") or []):
        if go.get("symbol") == symbol:
            return f"{symbol} ОТСЕЯНО воротами в {p.get('run_id')}: {', '.join(c for c, _ in go.get('fails', []))}. [{f.name}]"
    return f"{symbol} в последнем прогоне {p.get('run_id')} не встречался."


def sealed_summary():
    """Сколько запечатано по видам (kind) — из неизменяемого журнала прогнозов."""
    from mathlib import sealing as SEAL
    preds = SEAL.read_predictions()
    c = Counter(p.get("kind") for p in preds)
    return (f"Запечатано всего {len(preds)}: " + ", ".join(f"{k}={n}" for k, n in c.most_common())
            + ". [journal предсказаний, П16]")


def relevant_fact(q):
    """Роутер: вопрос → нужная интроспекция. None — фактовой привязки нет (свободный диалог)."""
    ql = (q or "").lower()
    sym = _ticker(q)
    if any(w in ql for w in ("brier", "ворот", "270", "калибров")):
        return brier_tracks()
    if sym and any(w in ql for w in ("borrow", "сквиз", "шорт", "short", "заём", "занять")):
        return borrow(sym)
    if any(w in ql for w in ("запечат", "seal", "сколько прогноз")):
        return sealed_summary()
    if sym and any(w in ql for w in ("почему", "отсе", "узл", "edge", "ярус", "трек", "score")):
        return why_node(sym)
    if any(w in ql for w in ("граф", "каскад", "сегодня", "прогон", "последн", "идеи дня", "топ")):
        return latest_graph()
    if sym:
        return why_node(sym)
    return None
