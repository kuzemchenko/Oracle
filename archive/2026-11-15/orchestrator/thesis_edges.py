# -*- coding: utf-8 -*-
"""orchestrator/thesis_edges.py — V6 (карта v3): рёбра-кандидаты из ТЕЗИСОВ ВЛАДЕЛЬЦА
в реестр форвард-теста B4, с провенансом.

Контур: курируемый конфиг knowledge/thesis_edges.yaml (тезис + механизм + источник →
инструмент + словесная причинность; правки только через git) → валидация КОДОМ
(тикеры в универсуме, тезис в theses_owner.yaml, механизм в mechanisms.yaml) →
append в knowledge/edge_candidates.jsonl (origin=thesis) — и дальше B4 (edge_forward,
крон 07:30) ежедневно форвард-тестит их наравне с библиотекой, forward_promotion
копит статистику ребра, идея из тезиса сможет печатать владельцу честный счёт.

П16: кандидат ≠ боевое ребро; lag=0 — «не измерен, зарабатывается тестом» (как Э4ж).
Дедуп: против библиотеки рёбер и против уже записанных кандидатов (edge_key).
Повторный прогон ничего не дублирует (идемпотентно).
"""
import argparse
import datetime
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "knowledge" / "thesis_edges.yaml"
THESES = ROOT / "knowledge" / "theses_owner.yaml"
MECHS = ROOT / "knowledge" / "mechanisms.yaml"
UNIVERSE = ROOT / "config" / "universe.yaml"
LAG_DAYS = 0          # не измерен — зарабатывается форвард-тестом B4 (как Э4ж)

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}\.US$")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def universe_tickers(path=None):
    doc = yaml.safe_load(pathlib.Path(path or UNIVERSE).read_text(encoding="utf-8")) or {}
    return set(re.findall(r"\b([A-Z0-9]{1,6}\.US)\b", json.dumps(doc, ensure_ascii=False)))


def load_config(path=None):
    doc = yaml.safe_load(pathlib.Path(path or CONFIG).read_text(encoding="utf-8")) or {}
    return list(doc.get("рёбра") or [])


def validate(rows, *, universe=None, theses_path=None, mechs_path=None):
    """Детерминированная проверка конфига. → (валидные, ошибки[]). Ошибка = ребро не идёт."""
    uni = universe if universe is not None else universe_tickers()
    th = set(((yaml.safe_load(pathlib.Path(theses_path or THESES).read_text(encoding="utf-8"))
               or {}).get("тезисы") or {}).keys())
    ms = {m.get("id") for m in ((yaml.safe_load(pathlib.Path(mechs_path or MECHS)
                                                .read_text(encoding="utf-8")) or {})
                                .get("механизмы") or [])}
    ok, errors = [], []
    seen = set()
    for i, r in enumerate(rows):
        tag = f"ребро#{i} {r.get('источник')}→{r.get('инструмент')}"
        src, dst = r.get("источник"), r.get("инструмент")
        if not (isinstance(src, str) and _TICKER_RE.match(src)):
            errors.append(f"{tag}: источник не тикер .US")
            continue
        if not (isinstance(dst, str) and _TICKER_RE.match(dst)):
            errors.append(f"{tag}: инструмент не тикер .US")
            continue
        if src == dst:
            errors.append(f"{tag}: источник = инструмент")
            continue
        if src not in uni or dst not in uni:
            errors.append(f"{tag}: вне универсума (нет котировок у сервера) — "
                          f"{'источник' if src not in uni else 'инструмент'}")
            continue
        if r.get("тезис") not in th:
            errors.append(f"{tag}: тезис «{r.get('тезис')}» не найден в theses_owner.yaml")
            continue
        if r.get("механизм") not in ms:
            errors.append(f"{tag}: механизм «{r.get('механизм')}» не найден в mechanisms.yaml")
            continue
        if not str(r.get("причинность") or "").strip():
            errors.append(f"{tag}: пустая причинность (П8: словесный механизм обязателен)")
            continue
        if (src, dst) in seen:
            errors.append(f"{tag}: дубль внутри конфига")
            continue
        seen.add((src, dst))
        ok.append(r)
    return ok, errors


def sync(config_path=None, *, registry_path=None, sens_path=None, universe=None,
         theses_path=None, mechs_path=None, now_dt=None):
    """Конфиг → реестр кандидатов (append-only, идемпотентно). Возвращает сводку."""
    from orchestrator import edge_forward as EFW
    from mathlib.calibration import forward_promotion as FP
    rows, errors = validate(load_config(config_path), universe=universe,
                            theses_path=theses_path, mechs_path=mechs_path)
    reg = pathlib.Path(registry_path) if registry_path else EFW.CANDIDATES
    lib_keys = {FP.edge_key(e["from"], e["to"], e["lag"])
                for e in EFW.edge_library(sens_path or EFW.SENS, candidates_path=None)}
    existing_keys = set()
    if reg.exists():
        for line in reg.read_text(encoding="utf-8").splitlines():
            try:
                existing_keys.add(json.loads(line).get("edge_key"))
            except (ValueError, AttributeError):
                continue
    now_iso = (now_dt or _now()).isoformat(timespec="seconds")
    added, dup_lib, dup_reg = [], 0, 0
    reg.parent.mkdir(parents=True, exist_ok=True)
    with open(reg, "a", encoding="utf-8") as f:
        for r in rows:
            key = FP.edge_key(r["источник"], r["инструмент"], LAG_DAYS)
            if key in lib_keys:
                dup_lib += 1
                continue
            if key in existing_keys:
                dup_reg += 1
                continue
            rec = {"ts": now_iso, "edge_key": key, "origin": "thesis",
                   "from": r["источник"], "to": r["инструмент"], "lag": LAG_DAYS,
                   "тезис": r["тезис"], "механизм": r["механизм"],
                   "причинность": r["причинность"],
                   **({"заметка": r["заметка"]} if r.get("заметка") else {}),
                   "провенанс": "V6 рёбра из тезисов владельца (карта v3, подпись 31.10); "
                                "лаг 0 — не измерен, зарабатывается форвард-тестом B4"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            existing_keys.add(key)
            added.append(f"{r['источник']}→{r['инструмент']}")
    return {"в_конфиге": len(rows) + len(errors), "валидных": len(rows),
            "добавлено": added, "дубль_библиотеки": dup_lib, "дубль_реестра": dup_reg,
            "ошибки": errors}


def main(argv=None):
    ap = argparse.ArgumentParser(description="V6: рёбра из тезисов → реестр кандидатов B4")
    ap.add_argument("--apply", action="store_true", help="дописать в реестр (иначе сухая проверка)")
    a = ap.parse_args(argv)
    if a.apply:
        s = sync()
    else:
        rows, errors = validate(load_config())
        s = {"валидных": len(rows), "ошибки": errors, "режим": "сухая проверка (--apply для записи)"}
    print(json.dumps(s, ensure_ascii=False, indent=1))
    return 1 if s.get("ошибки") else 0


if __name__ == "__main__":
    sys.exit(main())
