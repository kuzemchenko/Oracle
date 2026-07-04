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
import contextlib
import fcntl
import json
import hashlib
import datetime
import os
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


# ─── Межпроцессная синхронизация и внешний якорь (ревью 2026-07-04, Блок 1) ───
# Конкурентные писатели журнала РЕАЛЬНЫ (cron event_first --seal, /calibrate из бота, ручной
# funnel live). Без лока два одновременных seal() читают один prev_hash → вторая запись навсегда
# «бита» для verify_all и никогда не сверяется. Лок — fcntl.flock на сайдкар-файле <журнал>.lock.
#
# Якорь <журнал>.anchor.json = {count, last_hash} ВНЕ журнала: ловит усечение хвоста, которое
# hash-chain по построению не видит (удаление последних K строк оставляет валидную цепь).
# Якорь обновляется атомарно (tmp+replace) при каждом append под тем же локом.


@contextlib.contextmanager
def _locked(path):
    """Эксклюзивный межпроцессный лок на журнал (сайдкар <path>.lock, fcntl.flock)."""
    lock_path = pathlib.Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _anchor_path(path):
    return pathlib.Path(str(path) + ".anchor.json")


def _write_anchor(path, count, last_hash):
    """Атомарная запись якоря (tmp+os.replace) — обрыв процесса не оставит битый якорь."""
    ap = _anchor_path(path)
    tmp = ap.with_suffix(ap.suffix + ".tmp")
    payload = {"count": count, "last_hash": last_hash, "updated_at": now_utc_iso()}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, ap)


def verify_anchor(path=None, hash_field="hash"):
    """Сверить журнал с внешним якорем. Возвращает (ok, причина|None).

    Fail-closed: непустой журнал БЕЗ якоря — НЕ ок (после миграции каждый append пишет якорь;
    пропавший якорь = кто-то трогал журнал в обход seal/append_chained). Пустой журнал без
    якоря — ок (журнал ещё не заводили)."""
    path = pathlib.Path(path) if path is not None else PREDICTIONS_PATH
    recs = read_predictions(path)
    ap = _anchor_path(path)
    if not ap.exists():
        if not recs:
            return True, None
        return False, "якорь отсутствует при непустом журнале — усечение/правка в обход seal()"
    try:
        a = json.loads(ap.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "якорь нечитаем"
    if a.get("count") != len(recs):
        return False, (f"записей в журнале {len(recs)}, в якоре {a.get('count')} — "
                       f"усечение хвоста или дописка мимо цепочки")
    last = recs[-1].get(hash_field) if recs else None
    if a.get("last_hash") != last:
        return False, "hash хвоста журнала не совпадает с якорем"
    return True, None


def init_anchor(path=None, hash_field="hash"):
    """Одноразовая миграция: создать якорь для СУЩЕСТВУЮЩЕГО журнала (до этой версии якоря не было).
    Дальше якорь ведётся автоматически каждым seal()/append_chained()."""
    path = pathlib.Path(path) if path is not None else PREDICTIONS_PATH
    with _locked(path):
        recs = read_predictions(path)
        last = recs[-1].get(hash_field) if recs else None
        _write_anchor(path, len(recs), last)
    return {"count": len(recs), "last_hash": last}


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


def _content_hash(record, hash_field="hash"):
    """sha256 канонической (sort_keys) сериализации всего, КРОМЕ самого хэш-поля.
    Любая правка любого поля (включая sealed_at) меняет хэш → подделка задним числом видна.
    hash_field параметризован: у predictions это "hash", у outcomes — "rec_hash"
    (там "hash" занят ссылкой на прогноз)."""
    payload = {k: v for k, v in record.items() if k != hash_field}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _last_hash(path):
    """Hash последней записи журнала (для звена цепочки) или GENESIS, если журнал пуст."""
    recs = read_predictions(path)
    return recs[-1].get("hash") if recs else GENESIS_PREV_HASH


def seal(prediction, path=None, sealed_at=None, dedup_fields=None):
    """Запечатать прогноз и дописать ОДНУ строку в predictions.jsonl (только append).

    §9: неразрешимый прогноз → ValueError, в журнал НЕ попадает.
    sealed_at можно задать явно (детерминизм тестов); иначе текущее UTC-время.
    F2#21: проставляется prev_hash = hash предыдущей записи (hash-chain против удаления/перестановки).
    dedup_fields (ревью 2026-07-04): кортеж имён полей идентичности ставки — если запись с теми же
    значениями УЖЕ в журнале, повторно НЕ запечатываем и возвращаем None (идемпотентность
    перезапуска: дубли искусственно приближали ворота-270 и искажали Brier). Проверка — под тем же
    межпроцессным локом, что и append (гонка двух прогонов тоже закрыта).
    Возвращает запечатанную запись (с полями sealed_at, prev_hash и hash) или None (дубль).
    """
    problems = validate_resolvable(prediction)
    if problems:
        raise ValueError("прогноз неразрешим по §9, не запечатан: " + "; ".join(problems))
    path = pathlib.Path(path) if path is not None else PREDICTIONS_PATH
    record = dict(prediction)
    record.pop("hash", None)
    record["sealed_at"] = sealed_at or now_utc_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):                               # межпроцессный лок: read-last+append атомарны
        recs = read_predictions(path)
        if dedup_fields:
            key = tuple(record.get(f) for f in dedup_fields)
            if any(tuple(r.get(f) for f in dedup_fields) == key for r in recs):
                return None                           # та же ставка уже запечатана — не плодим дубль
        record["prev_hash"] = (recs[-1].get("hash") if recs else GENESIS_PREV_HASH)  # F2#21: звено цепочки
        record["hash"] = _content_hash(record)
        with open(path, "a", encoding="utf-8") as f:  # ТОЛЬКО append
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _write_anchor(path, len(recs) + 1, record["hash"])   # якорь против усечения хвоста
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


def verify_chain(path, hash_field="hash", prev_field="prev_hash", require_hash=True):
    """Универсальная проверка hash-цепочки jsonl-журнала. Возвращает (ok, индексы_битых).

    Правила:
    - запись с хэшем: content-hash обязан сходиться (любая правка видна);
    - запись со звеном (prev_field): звено обязано указывать на hash предыдущей записи
      (или GENESIS для первой) — ловит удаление/перестановку/вставку в середине;
    - ЛЕГАСИ-записи (без звена) допустимы ТОЛЬКО сплошным префиксом до начала цепочки.
      Ревью 2026-07-04 HIGH-2: раньше «легаси»-запись, дописанная в ХВОСТ, проходила verify —
      в цепь можно было вписать подделку. Теперь после первой записи со звеном любая запись
      без звена = битая;
    - require_hash=True (predictions): каждая запись обязана нести content-hash;
      require_hash=False (outcomes): записи до миграции не имеют rec_hash — легитимное легаси."""
    recs = read_predictions(path)
    bad = set()
    chain_started = False
    for i, r in enumerate(recs):
        stored = r.get(hash_field)
        pv = r.get(prev_field)
        if stored is None and pv is None:
            if require_hash or chain_started:
                bad.add(i)                            # для predictions хэш обязателен; легаси после цепи — подделка
            continue
        if stored is None or stored != _content_hash(r, hash_field):
            bad.add(i)                                # содержимое записи тронуто
            continue
        if pv is None:
            if chain_started:
                bad.add(i)                            # «легаси» ПОСЛЕ начала цепочки = вписанная подделка
            continue
        chain_started = True
        expected = (recs[i - 1].get(hash_field) if i > 0 else None) or GENESIS_PREV_HASH
        if pv != expected:
            bad.add(i)                                # звено цепочки разорвано → запись/соседи тронуты
    return (not bad, sorted(bad))


def verify_all(path=None):
    """Проверить целостность журнала прогнозов: (а) хэш каждой записи; (б) hash-chain (F2#21);
    (в) легаси-записи только сплошным префиксом (ревью 2026-07-04).
    Возвращает (ok, отсортированные_индексы_битых_записей).
    ВАЖНО: усечение ХВОСТА цепочка не видит по построению — его ловит verify_anchor()."""
    return verify_chain(path if path is not None else PREDICTIONS_PATH)


def append_chained(path, record, hash_field="rec_hash", prev_field="prev_rec_hash", lock=True):
    """Append-запись с hash-цепочкой и якорем для ПРОИЗВОЛЬНОГО jsonl-журнала (напр. outcomes.jsonl).

    Под межпроцессным локом: prev = hash последней записи (GENESIS, если журнал пуст или хвост —
    легаси без хэша), content-hash поверх всех полей включая звено, атомарное обновление якоря.
    lock=False — ТОЛЬКО если вызывающий уже держит _locked(path) (вложенный flock на другом fd
    того же лок-файла = дедлок). Возвращает записанную запись."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(record)
    rec.pop(hash_field, None)
    ctx = _locked(path) if lock else contextlib.nullcontext()
    with ctx:
        recs = read_predictions(path)
        rec[prev_field] = (recs[-1].get(hash_field) if recs else None) or GENESIS_PREV_HASH
        rec[hash_field] = _content_hash(rec, hash_field)
        with open(path, "a", encoding="utf-8") as f:  # ТОЛЬКО append
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _write_anchor(path, len(recs) + 1, rec[hash_field])
    return rec
