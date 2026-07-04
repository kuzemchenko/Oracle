# -*- coding: utf-8 -*-
"""orchestrator/attention_field.py — датчик перегретости КАК ИНФОРМАЦИОННОЕ ПОЛЕ идей (П2а,
REVISION_2026-07 §R4.2; подписано 04.07).

РОЛЬ. Каждой идее выдачи (картограф / треки графа) прикрепляется поле «внимание»: насколько тема
УЖЕ на радаре толпы по Google Trends (mathlib/attention, гейт gate-P1). Поле — информационное:
на ранжирование/отбор/суд-вердикт НЕ влияет (пере-ранжирование = П2б, слой Б, отдельная подпись
владельца). Судья видит его в проверяемом деле каскада как детерминированные данные (вход §8).

ПРАВИЛА ЧЕСТНОСТИ (подписаны, §R0#5):
  • `свежесть=None` ≠ `свежесть=1.0`: нет данных Trends — отдельная категория «не_измерено»
    с причиной, не награда и не приговор;
  • провенанс ключа журналируется (journal/attention_keys.jsonl, append-only);
  • пересдача ключа для той же идеи/актива ЗАПРЕЩЕНА: первый назначенный ключ — окончательный
    (иначе выбор ключа = степень свободы «подобрать удобный score»).

ОТКУДА КЛЮЧИ (детерминированно, Инв#6 — LLM здесь не вызывается):
  1) config/attention_keys.yaml — курируемые сиды (актив ядра → ключ его темы);
  2) journal/attention_keys.jsonl — реестр назначенных (первый выигрывает);
  3) кандидаты от вызывающего — ключевые слова НОВОСТНОГО КЛАСТЕРА, породившего идею картографа
     (готовые строки, детерминированно первая; назначение журналируется). Данные по новому ключу
     появятся после следующего суточного фетча (data/trends.py включает реестр в план фетча).
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mathlib import attention as A          # noqa: E402
from mathlib import sealing as SEAL         # noqa: E402  (межпроцессный лок реестра)
from data import trends as TR               # noqa: E402

SEEDS_PATH = ROOT / "config" / "attention_keys.yaml"
REGISTRY_PATH = ROOT / "journal" / "attention_keys.jsonl"

MAX_FETCH_AGE_DAYS = 7                       # старше — «фетч устарел», честный None (П8)
LATE_PHASES = ("ПОЗДНО", "ЛОВУШКА", "ОТЫГРАНО")   # §R5 sanity: в топе только с явной пометкой


def _load_seeds(path=None):
    import yaml
    p = pathlib.Path(path) if path else SEEDS_PATH
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in (data.get("assets") or {}).items()}


def _load_registry(path=None):
    """Реестр назначенных ключей: {актив: первая_запись} (первый выигрывает — пересдача запрещена).
    Битая строка (обрыв записи/ручная правка) НЕ валит реестр целиком (кросс-ревью П2а, HIGH):
    пропускается с громкой пометкой в stderr — идеи без записи честно получат «не_измерено»."""
    p = pathlib.Path(path) if path else REGISTRY_PATH
    if not p.exists():
        return {}
    out, broken = {}, 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                broken += 1
                continue
            # кросс-ревью №2: запись обязана быть ОБЪЕКТОМ с непустыми актив/ключ — валидный JSON
            # null/[]/строка ронял бы прогон AttributeError'ом, а объект без ключа затенял бы
            # (setdefault) более позднюю валидную запись того же актива
            if (not isinstance(rec, dict) or not str(rec.get("актив") or "").strip()
                    or not str(rec.get("ключ") or "").strip()):
                broken += 1
                continue
            # кросс-ревью №4: нормализация И при чтении — легаси-запись « ccj.us » обязана
            # участвовать в «первый выигрывает» для CCJ.US, иначе пересдача через регистр
            out.setdefault(str(rec["актив"]).strip().upper(), rec)   # первый ВАЛИДНЫЙ выигрывает
    if broken:
        print(f"⚠ attention_keys.jsonl: {broken} битых/невалидных строк пропущено "
              f"(реестр жив, проверь журнал)", file=sys.stderr)
    return out


def registry_keywords(path=None):
    """Все ключи реестра — для включения в план суточного фетча (data/trends.load_keywords)."""
    return sorted({r.get("ключ") for r in _load_registry(path).values() if r.get("ключ")})


def assign_key(asset, keyword, source, run_id, ts, path=None):
    """Назначить активу Trends-ключ с журналируемым провенансом. Если ключ УЖЕ назначен —
    возвращается существующая запись (пересдача запрещена, §R0#5), новая НЕ пишется.
    Проверка+append — под межпроцессным локом (кросс-ревью П2а, BLOCKER: два одновременных
    прогона могли назначить активу РАЗНЫЕ ключи, каждый посчитав поле по своему)."""
    asset = str(asset or "").strip().upper()       # LOW-1: нормализация — «ccj.us » не обходит запрет
    p = pathlib.Path(path) if path else REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with SEAL._locked(p):
        existing = _load_registry(p).get(asset)
        if existing:
            return existing
        rec = {"актив": asset, "ключ": str(keyword), "источник": source, "run_id": run_id, "ts": ts}
        with open(p, "a", encoding="utf-8") as f:      # append-only журнал провенанса
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def resolve_key(asset, *, seeds=None, registry=None):
    """(ключ, источник) для актива: РЕЕСТР → сиды → (None, None).

    Кросс-ревью П2а (HIGH): реестр (первое журналированное назначение) ВЫШЕ сидов — иначе
    поздняя правка attention_keys.yaml тихо пересдавала бы ключ мимо append-only журнала.
    Сид применяется только к активу, который ещё НИКОГДА не фиксировался, и при первом
    использовании сам журналируется (см. field_for_asset)."""
    seeds = _load_seeds() if seeds is None else seeds
    registry = _load_registry() if registry is None else registry
    rec = registry.get(asset)
    if rec and rec.get("ключ"):
        return rec["ключ"], f"реестр ({rec.get('источник')}, {rec.get('run_id')})"
    if asset in seeds:
        return seeds[asset], "seed config/attention_keys.yaml"
    return None, None


def _not_measured(reason, key=None, key_source=None):
    """Категория «не_измерено» — НЕ «свежесть=1.0» и НЕ штраф (§R0#5): честное отсутствие данных."""
    return {"статус": "не_измерено", "причина": reason, "ключ": key, "источник_ключа": key_source,
            "score": None, "свежесть": None, "фаза": None, "фетч_utc": None}


def field_for_asset(con, asset, *, asof, run_id, candidates=None,
                    seeds=None, registry_path=None, fix_keys=True):
    """Поле «внимание» для одного актива. candidates — детерминированные строки-кандидаты ключа
    (напр. ключевые слова кластера картографа): используются ТОЛЬКО если ключа ещё нет; назначение
    журналируется. Возвращает dict поля (статус ok | не_измерено).

    Кросс-ревью П2а (HIGH): реестр читается ТОЛЬКО из registry_path (инъекция готового dict
    позволяла посчитать поле по незажурналированному ключу — обход провенанса). Любой фактически
    ИСПОЛЬЗУЕМЫЙ ключ (включая сид при первом касании) фиксируется в append-only реестре —
    после этого правка сидов актив не пересдаёт (resolve_key: реестр выше сидов)."""
    asset = str(asset or "").strip().upper()       # LOW-1: единая нормализация актива
    registry = _load_registry(registry_path)
    key, key_source = resolve_key(asset, seeds=seeds, registry=registry)
    if key is not None and asset not in registry and fix_keys:
        rec = assign_key(asset, key, key_source, run_id, asof, path=registry_path)
        key = rec["ключ"]                      # гонка: другой прогон успел первым — его ключ окончателен
        key_source = f"реестр ({rec.get('источник')}, {rec.get('run_id')})"
    if key is None and candidates and fix_keys:
        # детерминизм (кросс-ревью LOW): множества сортируем; списки — в порядке значимости кластера
        cands = sorted(candidates) if isinstance(candidates, (set, frozenset)) else list(candidates)
        cand = next((str(c).strip() for c in cands if c and str(c).strip()), None)
        if cand:
            rec = assign_key(asset, cand, "ключи новостного кластера картографа",
                             run_id, asof, path=registry_path)
            key, key_source = rec["ключ"], f"реестр ({rec.get('источник')}, {rec.get('run_id')})"
    if key is None:
        return _not_measured("ключ Trends не назначен (нет сида/реестра/кандидатов)")

    rows = TR.rows_for_attention(con, key)
    if not rows:
        return _not_measured("нет канонических строк Trends по ключу — появятся после суточного фетча",
                             key=key, key_source=key_source)
    r = A.attention_from_rows(rows, asof=asof, max_age_days=MAX_FETCH_AGE_DAYS)
    if r.get("score") is None:
        return _not_measured(f"{r.get('провенанс')} — данные появятся после суточного фетча ключа",
                             key=key, key_source=key_source)
    field = {"статус": "ok", "ключ": key, "источник_ключа": key_source,
             "score": r["score"], "свежесть": r["свежесть"], "фаза": r["фаза"],
             "уровень": r["уровень"], "наклон": r["наклон"], "фетч_utc": r.get("фетч_utc"),
             "окно_trends": r.get("окно_trends"), "провенанс": r.get("провенанс")}
    if r["фаза"] in LATE_PHASES:
        # §R5 sanity: поздняя фаза внимания ОБЯЗАНА нести явную пометку на любой поверхности выдачи
        field["предупреждение"] = (f"фаза {r['фаза']}: тема уже на радаре толпы/отгремела — "
                                   f"поздно для «неочевидно+рано»")
    return field


def annotate_ideas(con, картограф_идеи, треки, *, asof, run_id,
                   seeds=None, registry_path=None, fix_keys=True):
    """Прикрепить поле «внимание» идеям выдачи (мутирует dict'ы) и вернуть покрытие (§R5).

    картограф_идеи: candidates = их «ключи» (слова кластера, журналируемое назначение);
    треки (money/provisional/digest_only): только сиды/реестр — кандидатов у узлов графа нет.
    Поле НЕ влияет на ранжирование: прикрепляется ПОСЛЕ отбора/маршрутизации.
    fix_keys=False (mock/dry): только ЧТЕНИЕ реестра/сидов, назначения НЕ журналируются
    (конвенция П16: mock журналы не трогает). Реестр перечитывается на каждый актив
    (объём — единицы идей на прогон)."""
    seeds = _load_seeds() if seeds is None else seeds
    ok = missing = 0
    for idea in (картограф_идеи or []):
        f = field_for_asset(con, idea.get("актив"), asof=asof, run_id=run_id,
                            candidates=idea.get("ключи"), seeds=seeds,
                            registry_path=registry_path, fix_keys=fix_keys)
        idea["внимание"] = f
        ok, missing = (ok + 1, missing) if f["статус"] == "ok" else (ok, missing + 1)
    for track in ("money", "provisional", "digest_only"):
        for s in (треки or {}).get(track, []) or []:
            f = field_for_asset(con, s.get("symbol"), asof=asof, run_id=run_id,
                                seeds=seeds, registry_path=registry_path, fix_keys=fix_keys)
            s["внимание"] = f
            ok, missing = (ok + 1, missing) if f["статус"] == "ok" else (ok, missing + 1)
    total = ok + missing
    return {"всего_идей": total, "с_данными": ok, "не_измерено": missing,
            "покрытие": (round(ok / total, 3) if total else None),
            "цель_§R5": "покрытие ≥0.60 (наблюдение, не гейт П2а)"}
