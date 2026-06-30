# -*- coding: utf-8 -*-
"""mathlib/sealing.py — запечатывание прогнозов (MASTER_SPEC §9, П16, инвариант 3 CLAUDE.md).

§9 «Стандарт разрешимости»: прогноз принимается в журнал ТОЛЬКО если однозначен ДО события —
актив/контракт, направление, величина (порог), срок, источник цены для сверки. «Нефть вырастет» —
не прогноз; «Brent ближайшей серии закроется ВЫШЕ $X до даты Y по данным Z» — прогноз.

Запечатывание: sealed_at (timestamp) + hash содержимого → подделка задним числом невозможна
(hash покрывает ВСЕ поля, включая sealed_at; любая правка меняет hash и ловится verify_seal).

Запись — ТОЛЬКО append одной строкой в journal/predictions.jsonl, ТОЛЬКО через seal().
Ничего не удаляется и не редактируется (хук guard_journal.py блокирует прямую правку журнала).
"""
import json
import hashlib
import datetime
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREDICTIONS_PATH = ROOT / "journal" / "predictions.jsonl"

# §9: однозначность ДО события.
REQUIRED_FIELDS = ("asset", "direction", "threshold", "resolve_by", "price_source")
VALID_DIRECTIONS = ("above", "below")

# F2#21 (§8.1): hash-chain. Каждая новая запись несёт prev_hash = hash ПРЕДЫДУЩЕЙ записи журнала
# (или GENESIS для первой в цепочке). prev_hash входит в content-hash → удаление/перестановка/вставка
# рвут цепочку и ловятся verify_all. Легаси-записи без prev_hash верифицируются по-старому (бэк-совм.):
# _content_hash хэширует ИМЕЮЩИЕСЯ поля, поэтому старые записи (без prev_hash) проходят как раньше.
GENESIS_PREV_HASH = "0" * 64


def now_utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def validate_resolvable(prediction):
    """Проверка §9. Возвращает СПИСОК проблем (пустой = прогноз разрешим).
    П8: лучше честно отказать в запечатывании, чем впустить неоднозначный прогноз."""
    problems = []
    if not isinstance(prediction, dict):
        return ["прогноз должен быть dict"]
    for f in REQUIRED_FIELDS:
        v = prediction.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            problems.append(f"нет поля: {f}")
    d = prediction.get("direction")
    if d is not None and str(d).strip().lower() not in VALID_DIRECTIONS:
        problems.append(f"direction должно быть из {VALID_DIRECTIONS}")
    thr = prediction.get("threshold")
    if thr is not None:
        try:
            float(thr)
        except (TypeError, ValueError):
            problems.append("threshold должно быть числом")
    p = prediction.get("probability")
    if p is not None:
        try:
            pf = float(p)
            if not 0.0 <= pf <= 1.0:
                problems.append("probability должно быть в [0,1]")
        except (TypeError, ValueError):
            problems.append("probability должно быть числом")
    return problems


def is_resolvable(prediction):
    return not validate_resolvable(prediction)


def _content_hash(record):
    """sha256 канонической (sort_keys) сериализации всего, КРОМЕ самого поля hash.
    Любая правка любого поля (включая sealed_at) меняет хэш → подделка задним числом видна."""
    payload = {k: v for k, v in record.items() if k != "hash"}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _last_hash(path):
    """Hash последней записи журнала (для звена цепочки) или GENESIS, если журнал пуст."""
    recs = read_predictions(path)
    return recs[-1].get("hash") if recs else GENESIS_PREV_HASH


def seal(prediction, path=None, sealed_at=None):
    """Запечатать прогноз и дописать ОДНУ строку в predictions.jsonl (только append).

    §9: неразрешимый прогноз → ValueError, в журнал НЕ попадает.
    sealed_at можно задать явно (детерминизм тестов); иначе текущее UTC-время.
    F2#21: проставляется prev_hash = hash предыдущей записи (hash-chain против удаления/перестановки).
    Возвращает запечатанную запись (с полями sealed_at, prev_hash и hash).
    """
    problems = validate_resolvable(prediction)
    if problems:
        raise ValueError("прогноз неразрешим по §9, не запечатан: " + "; ".join(problems))
    path = pathlib.Path(path) if path is not None else PREDICTIONS_PATH
    record = dict(prediction)
    record.pop("hash", None)
    record["sealed_at"] = sealed_at or now_utc_iso()
    record["prev_hash"] = _last_hash(path)            # F2#21: звено цепочки (входит в content-hash)
    record["hash"] = _content_hash(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:      # ТОЛЬКО append
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_predictions(path=None):
    """Прочитать журнал прогнозов (список dict в порядке записи)."""
    path = pathlib.Path(path) if path is not None else PREDICTIONS_PATH
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def verify_seal(record):
    """True, если хэш записи совпадает с пересчитанным (не подделана)."""
    stored = record.get("hash")
    if not stored:
        return False
    return stored == _content_hash(record)


def verify_all(path=None):
    """Проверить целостность всего журнала: (а) хэш каждой записи; (б) hash-chain (F2#21).

    Цепочка: у записи с prev_hash он ОБЯЗАН совпадать с hash предыдущей записи (или GENESIS для
    первой) — это ловит УДАЛЕНИЕ/ПЕРЕСТАНОВКУ/ВСТАВКУ, чего одиночный content-hash не видит.
    Легаси-записи без prev_hash цепочку не несут — их звено пропускаем (бэк-совместимость).
    Возвращает (ok, отсортированные_индексы_битых_записей)."""
    recs = read_predictions(path)
    bad = set()
    for i, r in enumerate(recs):
        if not verify_seal(r):
            bad.add(i)
            continue
        pv = r.get("prev_hash")
        if pv is not None:
            expected = recs[i - 1].get("hash") if i > 0 else GENESIS_PREV_HASH
            if pv != expected:
                bad.add(i)                            # звено цепочки разорвано → запись/соседи тронуты
    return (not bad, sorted(bad))
