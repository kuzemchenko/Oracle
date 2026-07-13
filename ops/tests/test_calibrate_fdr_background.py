# -*- coding: utf-8 -*-
"""Тесты драйвера Д1 ops/calibrate_fdr_background.py: сплайс thresholds.yaml сохраняет
прочие секции БАЙТ-В-БАЙТ, новая секция парсится, отсутствие маркеров — честный отказ.
Герметично: файлы — во временном каталоге, боевая БД не трогается."""
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import calibrate_fdr_background as CFB        # noqa: E402

OLD_TEXT = """# СГЕНЕРИРОВАНО ops/calibrate_week4.py (программа §23.1, честная зона walk-forward).
# Правки руками будут перезаписаны при следующем прогоне калибровки.
# Дата генерации: 2026-06-11T12:51:16Z

version: 2
calibrated: true
fdr:
  procedure: benjamini_hochberg
  q_value_max: 0.1
  word_frequency_background: null
  background_metrics:
    SPY.US:
      ret:
        std: 0.011145
        n: 2875
timing:
  spent_move_sigma: 1.5
  verdicts:
  - РАНО
manipulation:
  score_block_threshold: 7.0
"""

BG = {"SPY.US": {"ret": {"std": 0.01, "n": 100}},
      "NEW.US": {"insufficient_history": True, "n_bars": 10, "note": "нет фона (П8)"}}
TAIL = {"provenance": {"script": "ops/calibrate_fdr_background.py"},
        "fallback": {"ret_z_20": 30.0, "vol_z_log_20": 20.0, "vol_z_20": 3},
        "per_instrument": {"SPY.US": {"ret_z_20": 15.0, "vol_z_log_20": 20.0}},
        "unpinned": None}
WINDOW = {"from": "2015-01-02", "to": "2026-07-13"}


def test_splice_preserves_other_sections_byte_for_byte():
    new = CFB.splice_thresholds(OLD_TEXT, BG, TAIL, WINDOW, 2, 1)
    # хвост от timing: и тело от version: до background_metrics — байт-в-байт
    old_tail = OLD_TEXT[OLD_TEXT.index("timing:"):]
    assert new[new.index("timing:"):] == old_tail
    old_mid = OLD_TEXT[OLD_TEXT.index("version:"):OLD_TEXT.index("  background_metrics:")]
    assert old_mid in new
    # заголовок обновлён (провенанс нового скрипта), старый заголовок ушёл
    assert "calibrate_fdr_background.py" in new.split("\n")[0]
    assert "СГЕНЕРИРОВАНО ops/calibrate_week4.py" not in new


def test_splice_result_parses_with_new_sections():
    new = CFB.splice_thresholds(OLD_TEXT, BG, TAIL, WINDOW, 2, 1)
    obj = yaml.safe_load(new)
    assert obj["fdr"]["q_value_max"] == 0.1                       # q НЕ тронут (B5/§6)
    assert obj["fdr"]["background_metrics"]["NEW.US"]["insufficient_history"] is True
    assert obj["fdr"]["tail_df"]["per_instrument"]["SPY.US"]["ret_z_20"] == 15.0
    assert obj["fdr"]["tail_df"]["fallback"]["vol_z_20"] == 3     # константа F2#19 в фолбэке
    assert obj["timing"]["spent_move_sigma"] == 1.5               # прочие секции целы
    assert obj["manipulation"]["score_block_threshold"] == 7.0


def test_splice_missing_markers_fails_closed():
    with pytest.raises(SystemExit):
        CFB.splice_thresholds("version: 2\nfdr:\n  q_value_max: 0.1\n", BG, TAIL, WINDOW, 1, 0)


# ── Д1 #6: точечный сплайс сохраняет ключи fdr ПОСЛЕ background_metrics ──────────────

OLD_TEXT_KEYS_AFTER_BG = """# СГЕНЕРИРОВАНО ops/calibrate_week4.py
# Дата генерации: 2026-06-11T12:51:16Z

version: 2
calibrated: true
fdr:
  procedure: benjamini_hochberg
  background_metrics:
    SPY.US:
      ret:
        std: 0.011145
        n: 2875
  q_value_max: 0.1
  min_sources: 2
timing:
  spent_move_sigma: 1.5
manipulation:
  score_block_threshold: 7.0
"""


def test_splice_preserves_fdr_keys_after_background_metrics():
    """Кросс-ревью Д1 HIGH: прежний сплайс вырезал весь диапазон background_metrics→timing и
    терял fdr.q_value_max/min_sources, стоящие ПОСЛЕ background_metrics. Точечный сплайс их хранит."""
    new = CFB.splice_thresholds(OLD_TEXT_KEYS_AFTER_BG, BG, TAIL, WINDOW, 2, 1)
    obj = yaml.safe_load(new)
    assert obj["fdr"]["q_value_max"] == 0.1                  # НЕ исчез (контракт «q=0.1 не трогаем»)
    assert obj["fdr"]["min_sources"] == 2                    # посторонний ключ fdr сохранён
    assert obj["fdr"]["background_metrics"]["SPY.US"]["ret"]["std"] == 0.01   # заменён на новый
    assert obj["fdr"]["tail_df"]["per_instrument"]["SPY.US"]["ret_z_20"] == 15.0  # добавлен
    assert obj["timing"]["spent_move_sigma"] == 1.5
    assert obj["manipulation"]["score_block_threshold"] == 7.0


def test_splice_idempotent_replaces_existing_tail_df():
    """Повторный прогон: существующий tail_df заменяется, прочие ключи fdr целы (не дублируются)."""
    once = CFB.splice_thresholds(OLD_TEXT_KEYS_AFTER_BG, BG, TAIL, WINDOW, 2, 1)
    twice = CFB.splice_thresholds(once, BG, TAIL, WINDOW, 2, 1)
    obj = yaml.safe_load(twice)
    assert obj["fdr"]["q_value_max"] == 0.1 and obj["fdr"]["min_sources"] == 2
    assert twice.count("tail_df:") == 1                      # ровно один блок tail_df
    assert obj["fdr"]["tail_df"]["per_instrument"]["SPY.US"]["ret_z_20"] == 15.0


# ── Д1 #2: гард стабильности ловит ЛЮБОЕ расхождение full-vs-pre-window ──────────────

def _section(per_instrument, fb_ret=30.0):
    return {"provenance": {}, "per_instrument": per_instrument,
            "fallback": {"ret_z_20": fb_ret, "vol_z_log_20": 20.0, "vol_z_20": 3},
            "unpinned": None}


def test_stability_guard_catches_all_divergences():
    full = _section({"A.US": {"ret_z_20": 5.0}, "B.US": {"ret_z_20": 8.0},
                     "D.US": {"ret_z_20": 6.0}})
    pre = _section({"A.US": {"ret_z_20": 5.0},                 # без изменений
                    "C.US": {"ret_z_20": 4.0},                 # пин появился только на будущих данных
                    "D.US": {"ret_z_20": 10.0}}, fb_ret=25.0)  # смена df + смена фолбэка; B.US пропал
    st = CFB.compute_stability(full, pre)
    pi = st["per_instrument"]
    assert pi["B.US"]["ret_z_20"]["тип"] == "пин_появился_на_будущих_данных"  # есть в full, нет в pre
    assert pi["C.US"]["ret_z_20"]["тип"] == "пин_пропал_без_будущих_данных"   # есть в pre, нет в full
    assert pi["D.US"]["ret_z_20"]["тип"] == "df_сменился"
    assert "A.US" not in pi                                    # совпадает — не расхождение
    assert st["fallback"]["ret_z_20"] == {"df_полная_история": 30.0, "df_до_2026-06-20": 25.0}
    assert st["n"] == 4                                        # 3 per-instrument + 1 фолбэк


def test_live_thresholds_yaml_consistent_with_base():
    """Регрессия на РЕАЛЬНЫЙ перегенерированный config/thresholds.yaml: прочие секции
    объектно совпадают с git-версией se-d1-base, fdr.tail_df добавлена."""
    pytest.skip("Д1 деактивирован в боевом config/thresholds.yaml до прохождения гейта se-d1 "
                "(откат 13.07: unreviewed-калибровка не работает в бою). Снять skip после гейта "
                "Д1 + перегенерации драйвером ops/calibrate_fdr_background.py.")
    import subprocess
    new = yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text(encoding="utf-8"))
    got = subprocess.run(["git", "show", "se-d1-base:config/thresholds.yaml"],
                         capture_output=True, text=True, cwd=str(ROOT))
    if got.returncode != 0:
        pytest.skip("git-версия se-d1-base недоступна")
    old = yaml.safe_load(got.stdout)
    for k in old:
        if k != "fdr":
            assert old[k] == new[k], f"секция {k} изменилась"
    for k in old["fdr"]:
        if k != "background_metrics":
            assert old["fdr"][k] == new["fdr"][k], f"fdr.{k} изменился"
    td = new["fdr"].get("tail_df") or {}
    assert td.get("per_instrument"), "нет fdr.tail_df.per_instrument"
    assert td.get("fallback", {}).get("ret_z_20"), "нет фолбэка ret_z_20"
