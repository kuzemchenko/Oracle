# -*- coding: utf-8 -*-
"""Тесты поля «внимание» (П2а, REVISION_2026-07 §R4.2): маппинг ключей, правила честности,
провенанс/запрет пересдачи, покрытие §R5, sanity-пометка поздних фаз."""
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

from orchestrator import attention_field as AF   # noqa: E402
from mathlib import attention as A               # noqa: E402
import news_common as nc                         # noqa: E402

ASOF = "2026-07-04T09:00:00+00:00"
FETCH = "2026-07-04T07:00:00+00:00"


def _con_with_series(key, values, timeframe=None):
    con = sqlite3.connect(":memory:")
    con.executescript(nc.SCHEMA)
    tf = timeframe or A.TRENDS_TIMEFRAME
    for i, v in enumerate(values):
        con.execute("INSERT INTO trends (keyword,geo,date,interest,is_partial,source,fetched_at,timeframe)"
                    " VALUES (?,?,?,?,0,'google_trends',?,?)",
                    (key, "", f"2026-06-{i+1:02d}", v, FETCH, tf))
    return con


def test_seed_asset_measured_and_seed_use_is_journaled(tmp_path):
    reg = tmp_path / "reg.jsonl"
    con = _con_with_series("brent oil", [10, 15, 20, 30, 45, 60, 80, 95])
    f = AF.field_for_asset(con, "BNO.US", asof=ASOF, run_id="t",
                           seeds={"BNO.US": "brent oil"}, registry_path=reg)
    assert f["статус"] == "ok" and f["ключ"] == "brent oil"
    assert f["score"] is not None and f["фетч_utc"] == FETCH
    # кросс-ревью HIGH: первое ИСПОЛЬЗОВАНИЕ сида журналируется — ключ фиксируется в реестре
    rec = json.loads(reg.read_text().splitlines()[0])
    assert rec["актив"] == "BNO.US" and rec["ключ"] == "brent oil"
    assert "seed" in rec["источник"]


def test_seed_change_does_not_resell_key(tmp_path):
    # Кросс-ревью HIGH: поздняя правка сидов НЕ пересдаёт уже зафиксированный ключ (реестр выше).
    reg = tmp_path / "reg.jsonl"
    con = _con_with_series("brent oil", [10, 15, 20, 30, 45, 60, 80, 95])
    AF.field_for_asset(con, "BNO.US", asof=ASOF, run_id="t",
                       seeds={"BNO.US": "brent oil"}, registry_path=reg)
    f2 = AF.field_for_asset(con, "BNO.US", asof=ASOF, run_id="t2",
                            seeds={"BNO.US": "совсем другой ключ"}, registry_path=reg)
    assert f2["ключ"] == "brent oil"                          # первый зафиксированный — окончательный
    assert len(reg.read_text().splitlines()) == 1


def test_no_key_is_not_measured_not_fresh(tmp_path):
    # §R0#5: нет ключа → отдельная категория «не_измерено»; свежесть НЕ 1.0 и НЕ 0.0 — None.
    con = _con_with_series("brent oil", [10, 20])
    f = AF.field_for_asset(con, "XYZ.US", asof=ASOF, run_id="t",
                           seeds={}, registry_path=tmp_path / "reg.jsonl")
    assert f["статус"] == "не_измерено"
    assert f["свежесть"] is None and f["score"] is None
    assert "не назначен" in f["причина"]


def test_candidates_assign_key_with_provenance_and_no_reassign(tmp_path):
    reg = tmp_path / "reg.jsonl"
    con = _con_with_series("uranium squeeze", [10, 20, 30])   # мало истории — но ключ назначится
    f = AF.field_for_asset(con, "CCJ.US", asof=ASOF, run_id="run1",
                           candidates=["uranium squeeze", "uranium"],
                           seeds={}, registry_path=reg)
    assert f["ключ"] == "uranium squeeze"                     # первый кандидат, детерминированно
    assert f["статус"] == "не_измерено"                       # данных мало — честно
    rec = json.loads(reg.read_text().splitlines()[0])
    assert rec == {"актив": "CCJ.US", "ключ": "uranium squeeze",
                   "источник": "ключи новостного кластера картографа", "run_id": "run1", "ts": ASOF}
    # запрет пересдачи: другие кандидаты НЕ переназначают ключ
    f2 = AF.field_for_asset(con, "CCJ.US", asof=ASOF, run_id="run2",
                            candidates=["совсем другой ключ"], seeds={}, registry_path=reg)
    assert f2["ключ"] == "uranium squeeze"
    assert len(reg.read_text().splitlines()) == 1             # вторая запись НЕ появилась


def test_candidates_set_is_deterministic(tmp_path):
    # Кросс-ревью LOW: set кандидатов сортируется — выбор не зависит от PYTHONHASHSEED.
    con = _con_with_series("x", [10, 20])
    f = AF.field_for_asset(con, "AAA.US", asof=ASOF, run_id="t",
                           candidates={"zzz key", "aaa key"}, seeds={},
                           registry_path=tmp_path / "reg.jsonl")
    assert f["ключ"] == "aaa key"


def test_broken_registry_line_does_not_kill_registry(tmp_path):
    # Кросс-ревью HIGH: битая строка журнала пропускается, остальные записи живы.
    reg = tmp_path / "reg.jsonl"
    good = {"актив": "BNO.US", "ключ": "brent oil", "источник": "s", "run_id": "r", "ts": ASOF}
    reg.write_text('{"актив":"CCJ.US","ключ":\n' + json.dumps(good, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    registry = AF._load_registry(reg)
    assert registry.get("BNO.US", {}).get("ключ") == "brent oil"
    assert "CCJ.US" not in registry
    assert AF.registry_keywords(reg) == ["brent oil"]


def test_late_phase_carries_warning(tmp_path):
    # §R5 sanity: ПОЗДНО/ЛОВУШКА/ОТЫГРАНО — только с явной пометкой.
    con = _con_with_series("copper price", [5, 8, 10, 20, 40, 70, 95, 96, 97, 96])  # плато у пика
    f = AF.field_for_asset(con, "CPER.US", asof=ASOF, run_id="t",
                           seeds={"CPER.US": "copper price"}, registry_path=tmp_path / "reg.jsonl")
    assert f["статус"] == "ok" and f["фаза"] in AF.LATE_PHASES
    assert "предупреждение" in f


def test_stale_fetch_is_not_measured(tmp_path):
    con = _con_with_series("brent oil", [10, 15, 20, 30, 45, 60, 80, 95])
    f = AF.field_for_asset(con, "BNO.US", asof="2026-08-01T00:00:00+00:00", run_id="t",
                           seeds={"BNO.US": "brent oil"}, registry_path=tmp_path / "reg.jsonl")
    assert f["статус"] == "не_измерено" and "устарел" in f["причина"]


def test_annotate_ideas_coverage_and_mutation(tmp_path):
    con = _con_with_series("brent oil", [10, 15, 20, 30, 45, 60, 80, 95])
    карто = [{"актив": "CCJ.US", "ключи": ["uranium squeeze"]}]
    треки = {"money": [{"symbol": "BNO.US", "node": {}}],
             "provisional": [{"symbol": "NUE.US", "node": {}}], "digest_only": []}
    cov = AF.annotate_ideas(con, карто, треки, asof=ASOF, run_id="t",
                            seeds={"BNO.US": "brent oil"}, registry_path=tmp_path / "reg.jsonl")
    assert треки["money"][0]["внимание"]["статус"] == "ok"            # сид + данные
    assert треки["provisional"][0]["внимание"]["статус"] == "не_измерено"  # без сида — честно
    assert карто[0]["внимание"]["ключ"] == "uranium squeeze"          # назначен из кластера
    assert cov["всего_идей"] == 3 and cov["с_данными"] == 1
    assert cov["покрытие"] == round(1 / 3, 3)


def test_field_never_affects_ranking_inputs(tmp_path):
    # П2а-инвариант: поле — информационное: ранжирующий score узла не тронут, датчик живёт
    # ТОЛЬКО внутри вложенного dict «внимание» (кросс-ревью LOW: прежний assert был тавтологией).
    con = _con_with_series("brent oil", [10, 15, 20, 30, 45, 60, 80, 95])
    s = {"symbol": "BNO.US", "node": {}, "score": 0.77, "prerank": {"reliability": "A"}}
    треки = {"money": [s], "provisional": [], "digest_only": []}
    AF.annotate_ideas(con, [], треки, asof=ASOF, run_id="t",
                      seeds={"BNO.US": "brent oil"}, registry_path=tmp_path / "reg.jsonl")
    assert s["score"] == 0.77 and s["prerank"] == {"reliability": "A"}   # входы ранжирования не тронуты
    assert set(s.keys()) == {"symbol", "node", "score", "prerank", "внимание"}  # только новое поле
    assert s["внимание"]["score"] is not None                            # датчик — внутри поля


def test_mock_mode_reads_but_does_not_journal(tmp_path):
    # Конвенция П16: mock журналы не трогает — fix_keys=False читает сиды/реестр, но НЕ пишет.
    reg = tmp_path / "reg.jsonl"
    con = _con_with_series("brent oil", [10, 15, 20, 30, 45, 60, 80, 95])
    f = AF.field_for_asset(con, "BNO.US", asof=ASOF, run_id="mock",
                           seeds={"BNO.US": "brent oil"}, registry_path=reg, fix_keys=False)
    assert f["статус"] == "ok"                                # поле посчитано (чтение)
    assert not reg.exists()                                   # но реестр НЕ создан
    f2 = AF.field_for_asset(con, "CCJ.US", asof=ASOF, run_id="mock",
                            candidates=["uranium"], seeds={}, registry_path=reg, fix_keys=False)
    assert f2["статус"] == "не_измерено" and not reg.exists() # кандидаты в mock не назначаются


def test_registry_rejects_nonobject_and_keyless_records(tmp_path):
    # Кросс-ревью №2: валидный JSON null/[]/строка и объект БЕЗ ключа — невалидные записи:
    # пропускаются и НЕ затеняют более позднюю валидную запись того же актива.
    reg = tmp_path / "reg.jsonl"
    good = {"актив": "BNO.US", "ключ": "brent oil", "источник": "s", "run_id": "r", "ts": ASOF}
    reg.write_text("null\n[]\n\"строка\"\n"
                   '{"актив":"BNO.US"}\n' + json.dumps(good, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    registry = AF._load_registry(reg)
    assert registry.get("BNO.US", {}).get("ключ") == "brent oil"   # валидная запись НЕ затенена
    assert AF.registry_keywords(reg) == ["brent oil"]


def test_asset_normalized_no_reassign_via_case(tmp_path):
    # LOW-1: «ccj.us » и «CCJ.US» — один актив: регистр/пробелы не обходят запрет пересдачи.
    reg = tmp_path / "reg.jsonl"
    con = _con_with_series("x", [10, 20])
    AF.field_for_asset(con, "CCJ.US", asof=ASOF, run_id="r1",
                       candidates=["uranium"], seeds={}, registry_path=reg)
    f2 = AF.field_for_asset(con, " ccj.us ", asof=ASOF, run_id="r2",
                            candidates=["другой ключ"], seeds={}, registry_path=reg)
    assert f2["ключ"] == "uranium"
    assert len(reg.read_text().splitlines()) == 1


def test_event_first_mock_integration_field_and_coverage():
    # LOW-4(2): автотест интеграции — поле в брифах протокола, покрытие в протоколе,
    # порядок annotate→суд гарантирован наличием поля у money-узлов ДО среза протокола.
    from orchestrator import event_first as EF
    r = EF.run_event_first(mode="mock", k=1, write=False)
    cov = r.get("внимание_покрытие")
    assert cov and ("покрытие" in cov or "ошибка" in cov)
    briefs = (r.get("граф_отбор") or {}).get("топ_k") or []
    assert briefs and all("внимание" in b for b in briefs)


def test_scan_keywords_exclude_attention_keys(tmp_path, monkeypatch):
    # HIGH-3: скан-ключи = только конфиг; ключи реестра «внимания» НЕ попадают в FDR-скан.
    import data.trends as T
    reg = tmp_path / "reg.jsonl"
    AF.assign_key("CCJ.US", "uranium squeeze", "тест", "r", ASOF, path=reg)
    monkeypatch.setattr(AF, "REGISTRY_PATH", reg)
    scan = T.scan_keywords()
    plan, *_ = T.load_keywords()
    assert "uranium squeeze" not in scan          # в скан/FDR не идёт (боковой канал закрыт)
    assert "uranium squeeze" in plan              # но фетчится для датчика


def test_fetch_cap_limits_registry_keys(tmp_path, monkeypatch):
    # HIGH-4: кэп MAX_ATTENTION_FETCH_KEYS — в план фетча идут ПОСЛЕДНИЕ ключи реестра.
    import data.trends as T
    reg = tmp_path / "reg.jsonl"
    for i in range(T.MAX_ATTENTION_FETCH_KEYS + 5):
        AF.assign_key(f"A{i}.US", f"key {i:03d}", "тест", "r", ASOF, path=reg)
    monkeypatch.setattr(AF, "REGISTRY_PATH", reg)
    plan, *_ = T.load_keywords()
    assert "key 000" not in plan                  # старшие выпали из окна фетча
    assert f"key {T.MAX_ATTENTION_FETCH_KEYS + 4:03d}" in plan   # свежие — в плане


def test_attention_line_render_none_safe():
    # LOW-4(3): рендер поля — None-safe и показывает предупреждение поздней фазы.
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "ops"))
    import bot_reports as R
    assert R._attention_line({}) == []                                    # старый протокол — молчим
    ok = {"внимание": {"статус": "ok", "ключ": "brent oil", "фаза": "ОТЫГРАНО",
                       "свежесть": 0.88, "предупреждение": "фаза ОТЫГРАНО: тема отгремела"}}
    lines = R._attention_line(ok)
    assert any("brent oil" in l for l in lines) and any("⚠️" in l for l in lines)
    nm = {"внимание": {"статус": "не_измерено", "причина": "ключ Trends не назначен"}}
    assert any("не измерено" in l for l in R._attention_line(nm))


def test_legacy_nonnormalized_registry_record_still_wins(tmp_path):
    # Кросс-ревью №4: легаси-запись « ccj.us » участвует в «первый выигрывает» для CCJ.US.
    reg = tmp_path / "reg.jsonl"
    rec = {"актив": " ccj.us ", "ключ": "uranium squeeze", "источник": "old", "run_id": "r1", "ts": ASOF}
    reg.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    con = _con_with_series("x", [10, 20])
    f = AF.field_for_asset(con, "CCJ.US", asof=ASOF, run_id="r2",
                           candidates=["другой ключ"], seeds={}, registry_path=reg)
    assert f["ключ"] == "uranium squeeze"                     # легаси-фиксация уважена
    assert len(reg.read_text().splitlines()) == 1             # новая запись НЕ дописана


def test_string_candidates_is_single_key_not_chars(tmp_path):
    # Кросс-ревью №5: строка-кандидат — один ключ, не список символов («u» не фиксируется навсегда).
    reg = tmp_path / "reg.jsonl"
    con = _con_with_series("x", [10, 20])
    f = AF.field_for_asset(con, "CCJ.US", asof=ASOF, run_id="r",
                           candidates="uranium squeeze", seeds={}, registry_path=reg)
    assert f["ключ"] == "uranium squeeze"
