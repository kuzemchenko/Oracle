# -*- coding: utf-8 -*-
"""Тесты датчика перегретости/неочевидности (mathlib/attention.py, П1)."""
from mathlib import attention as A


def test_cold_theme_low_score_phase_rano():
    # Тема вне радара БЕЗ следа отгремевшего хайпа: низкий шумный интерес → свежо, фаза РАНО.
    interest = [8, 10, 9, 11, 10, 12, 11, 13, 12, 13]
    r = A.attention_score(interest)
    assert r["score"] is not None
    assert r["свежесть"] > 0.5           # неочевидно
    assert r["фаза"] == "РАНО"
    assert 0.0 <= r["score"] <= 1.0


def test_deflated_old_hype_is_otygrano_not_rano():
    # П1-гейт 04.07 (в): хайп был ДАВНО (пик в начале окна) и сдулся более чем вдвое → это
    # ОТЫГРАННЫЙ сюжет, а не «рано»; раньше читалось как РАНО с максимальной «свежестью» —
    # инверсия сигнала ровно на целевом классе «неочевидно+рано».
    interest = [80, 70, 60, 40, 30, 20, 15, 12, 10, 8]
    r = A.attention_score(interest)
    assert r["фаза"] == "ОТЫГРАНО"
    assert r["score"] is not None and r["score"] <= A.LEVEL_COLD


def test_lone_spike_noise_is_not_otygrano():
    # Одиночный шум-спайк само-нормировки (одна точка у пика) — не хайп: низкоинтересный ряд
    # со случайным ранним максимумом остаётся РАНО (устойчивость хайпа требует ≥2 точек у пика).
    interest = [10, 100, 11, 9, 12, 10, 11, 12, 10, 11]
    r = A.attention_score(interest)
    assert r["фаза"] == "РАНО"


def test_two_isolated_spikes_are_not_hype():
    # Stage-review H-1: ДВА изолированных выброса (типичный шум разреженного ряда нишевого ключа —
    # ровно целевой класс «неочевидных» тем) — не «устойчивый хайп»: нужно ≥2 СМЕЖНЫХ точек у пика.
    interest = [10, 100, 9, 75, 11, 10, 9, 10, 11, 10]
    r = A.attention_score(interest)
    assert r["фаза"] == "РАНО"
    # а настоящий хайп (смежные точки у пика) при тех же прочих условиях — ОТЫГРАНО
    interest2 = [95, 100, 60, 30, 20, 15, 12, 10, 11, 10]
    r2 = A.attention_score(interest2)
    assert r2["фаза"] == "ОТЫГРАНО"


def test_unreadable_staleness_stamps_are_visible():
    # Stage-review L-4 (П8): отказ staleness-проверки не молчит — пометка в «заметке».
    rows = [("2026-05-%02d" % d, 10 + d * 5, 0, "не-дата") for d in range(1, 10)]
    r = A.attention_from_rows(rows, asof="тоже-не-дата", max_age_days=7)
    assert r["score"] is not None
    assert "ПРОПУЩЕНА" in r["заметка"]


def test_freshness_demanded_but_no_fetched_at_gives_none():
    # Кросс-ревью №2 (HIGH): требование свежести (asof+max_age_days) при строках БЕЗ fetched_at
    # не обходится молча — гарантировать свежесть нечем → честный None.
    rows = [("2020-01-%02d" % d, 10 + d, 0) for d in range(1, 10)]        # 3-элементные строки
    r = A.attention_from_rows(rows, asof="2026-07-04T09:00:00+00:00", max_age_days=7)
    assert r["score"] is None
    assert "нет метки свежести" in r["провенанс"]
    # без требования свежести те же строки считаются как раньше
    r2 = A.attention_from_rows(rows)
    assert r2["score"] is not None


def test_naive_aware_timestamp_mix_does_not_crash():
    # Кросс-ревью №2 (HIGH): naive fetched_at + aware asof раньше давали TypeError.
    rows = [("2026-05-%02d" % d, 10 + d * 5, 0, "2026-06-01T00:00:00") for d in range(1, 10)]
    stale = A.attention_from_rows(rows, asof="2026-07-04T09:00:00+00:00", max_age_days=7)
    assert stale["score"] is None and "устарел" in stale["провенанс"]     # naive = UTC, честно устарел
    fresh = A.attention_from_rows(rows, asof="2026-06-02T00:00:00", max_age_days=7)
    assert fresh["score"] is not None


def test_hot_peak_high_score_late():
    # Внимание на историческом пике окна и плато → перегрето, фаза ПОЗДНО.
    interest = [5, 8, 10, 20, 40, 70, 95, 96, 97, 96]
    r = A.attention_score(interest)
    assert r["score"] > 0.6
    assert r["свежесть"] < 0.4
    assert r["уровень"] >= A.LEVEL_HOT
    assert r["фаза"] in ("ПОЗДНО", "ВОВРЕМЯ")   # высоко; плато→ПОЗДНО, ещё ускоряется→ВОВРЕМЯ


def test_blowoff_rollover_is_trap():
    # Взлетело на недавний пик и перекатывается вниз (пик пройден) → ЛОВУШКА.
    interest = [10, 15, 30, 55, 80, 100, 95, 78, 60, 45]
    r = A.attention_score(interest)
    assert r["фаза"] == "ЛОВУШКА"
    assert r["наклон"] < 0                # честно остывает


def test_rising_from_low_is_early_or_timely():
    # Разгорается из низкого — импульс вверх, но уровень ещё не на пике.
    interest = [10, 9, 11, 10, 12, 14, 18, 22, 28, 33]
    r = A.attention_score(interest)
    assert r["наклон"] > 0
    assert r["фаза"] in ("РАНО", "ВОВРЕМЯ")


def test_insufficient_history_returns_none():
    r = A.attention_score([10, 20, 30])
    assert r["score"] is None
    assert r["фаза"] is None
    assert "мало истории" in r["провенанс"]


def test_flat_series_no_signal():
    # Плоский ряд: относительного сигнала внимания нет → честный None (П8), не выдумка.
    r = A.attention_score([50] * 10)
    assert r["score"] is None
    assert "вариаци" in r["провенанс"].lower() or "плоск" in r["провенанс"].lower()


def test_empty_input_none():
    r = A.attention_score([])
    assert r["score"] is None
    assert r["n"] == 0


def test_partial_tail_trimmed():
    # Последняя корзина Trends неполна (is_partial) и занижена — её нельзя принимать за «остывание».
    interest = [20, 30, 45, 60, 75, 88, 95, 97, 98, 40]
    flags = [False] * 9 + [True]        # последний — партиал (заниженный)
    r = A.attention_score(interest, is_partial=flags)
    assert "срезан незавершённый хвост" in r["заметка"]
    assert r["n"] == 9                  # партиал-хвост отброшен
    # без ложного партиала последнее полное значение высокое → перегрето, не ЛОВУШКА
    assert r["уровень"] >= A.LEVEL_HOT


def test_partial_flags_filtered_jointly_with_values():
    # П1-гейт 04.07 (а): None в ряду выпадает ВМЕСТЕ со своим partial-флагом. Раньше vals чистился,
    # а флаги нет — списки съезжали, и _trim_partial срезал ПОЛНОЦЕННУЮ последнюю корзину.
    interest = [20, 30, 40, 50, 60, 70, 80, 90, None]
    flags = [False] * 8 + [True]                     # партиал — ровно у None-точки
    r = A.attention_score(interest, is_partial=flags)
    assert r["n"] == 8                               # полноценные корзины НЕ срезаны
    assert r["уровень"] >= A.LEVEL_HOT               # 90 не потерян → горячо, не «остывание»
    # обратная сторона: реальный партиал-хвост режется и при None в середине ряда
    interest2 = [20, 30, None, 50, 60, 70, 80, 95, 40]
    flags2 = [False, False, False, False, False, False, False, False, True]
    r2 = A.attention_score(interest2, is_partial=flags2)
    assert "срезан незавершённый хвост" in r2["заметка"]
    assert r2["n"] == 7                              # 9 − None − партиал


def test_partial_flags_length_mismatch_ignored_with_note():
    interest = [20, 30, 40, 50, 60, 70, 80, 90]
    r = A.attention_score(interest, is_partial=[True, False])   # рассинхрон — дефект вызова
    assert r["score"] is not None
    assert "рассинхрон" in r["заметка"]


def test_from_rows_uses_only_latest_fetch_normalization():
    # П1-гейт 04.07 (б): строки разных фетчей нормированы к РАЗНЫМ пикам окна — считаем только
    # по последнему фетчу (одна нормировка), иначе фаза — артефакт склейки.
    old = [("2026-03-%02d" % d, 100 - d * 10, 0, "2026-04-01T00:00:00Z") for d in range(1, 9)]
    new = [("2026-06-%02d" % d, 10 + d, 0, "2026-07-01T00:00:00Z") for d in range(1, 10)]
    r = A.attention_from_rows(old + new)
    assert r["n"] == 9                               # только строки последнего фетча
    assert r["пик_окна"] <= 20                       # старый «пик 90+» из чужой нормировки не участвует
    # без fetched_at (3-элементные строки) поведение прежнее — считаем по всем
    r2 = A.attention_from_rows([row[:3] for row in (old + new)])
    assert r2["n"] == 17


def test_output_carries_canonical_timeframe():
    r = A.attention_score([10, 20, 30, 40, 50, 60, 70, 80])
    assert r["окно_trends"] == A.TRENDS_TIMEFRAME


def test_plateau_peak_recent_drop_is_trap_not_otygrano():
    # Кросс-ревью П1-гейта: плато максимума (100,100,100,100) и свежий обвал — пик кончился
    # НЕДАВНО (последнее касание максимума), это ЛОВУШКА; argmax по первому вхождению давал
    # ложное «пик давно» → ОТЫГРАНО.
    interest = [20, 30, 60, 100, 100, 100, 100, 35, 20, 20]
    r = A.attention_score(interest)
    assert r["фаза"] == "ЛОВУШКА"


def test_uniform_output_contract_all_branches():
    # Единый набор ключей у успешной и «нет данных» ветвей — потребитель не ловит KeyError.
    ok = A.attention_score([10, 20, 30, 40, 50, 60, 70, 80])
    empty = A.attention_score([10, 20, 30])          # мало истории
    flat = A.attention_score([50] * 10)              # плоский
    assert set(ok.keys()) == set(empty.keys()) == set(flat.keys())


def test_stale_fetch_gives_none_with_asof():
    # Кросс-ревью: пустой свежий фетч не пишет строк — MAX(fetched_at) молча вернул бы старый ряд.
    # С asof+max_age_days устаревший фетч честно даёт score=None, а не фазу по старой нормировке.
    rows = [("2026-05-%02d" % d, 10 + d * 5, 0, "2026-06-01T00:00:00+00:00") for d in range(1, 10)]
    stale = A.attention_from_rows(rows, asof="2026-07-04T09:00:00+00:00", max_age_days=7)
    assert stale["score"] is None
    assert "устарел" in stale["провенанс"]
    assert stale["фетч_utc"] == "2026-06-01T00:00:00+00:00"   # провенанс сохранён
    fresh = A.attention_from_rows(rows, asof="2026-06-03T00:00:00+00:00", max_age_days=7)
    assert fresh["score"] is not None
    assert fresh["фетч_utc"] == "2026-06-01T00:00:00+00:00"


def test_score_bounds_and_freshness_complement():
    interest = [12, 18, 25, 40, 55, 70, 60, 50, 45, 42]
    r = A.attention_score(interest)
    assert 0.0 <= r["score"] <= 1.0
    assert abs(r["score"] + r["свежесть"] - 1.0) < 1e-9


def test_attention_map_groups_by_keyword():
    rows = [("brent oil", "2026-05-01", 10), ("brent oil", "2026-05-08", 20),
            ("brent oil", "2026-05-15", 30), ("brent oil", "2026-05-22", 45),
            ("brent oil", "2026-05-29", 60), ("brent oil", "2026-06-05", 80),
            ("brent oil", "2026-06-12", 95), ("brent oil", "2026-06-19", 97),
            ("uranium", "2026-06-01", 40), ("uranium", "2026-06-08", 42)]
    m = A.attention_map(rows)
    assert set(m.keys()) == {"brent oil", "uranium"}
    assert m["brent oil"]["score"] is not None      # 8 точек — хватает
    assert m["uranium"]["score"] is None            # 2 точки — честный None


def test_from_rows_sorts_by_date():
    # Строки вперемешку по дате — датчик обязан отсортировать перед расчётом импульса.
    rows = [("2026-06-19", 95), ("2026-05-01", 10), ("2026-06-05", 80),
            ("2026-05-15", 30), ("2026-05-29", 60), ("2026-05-08", 20),
            ("2026-06-12", 92), ("2026-05-22", 45)]
    r = A.attention_from_rows(rows)
    assert r["score"] is not None
    assert r["наклон"] > 0              # по возрастанию даты ряд растёт
