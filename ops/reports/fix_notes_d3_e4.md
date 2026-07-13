# Верификация находок ревью Д3 + Э4 (Поисковый движок)

Метод: каждую находку из `reports/cross_review_20260713T123743Z.md` (Д3) и
`reports/cross_review_20260713T124048Z.md` (Э4) воспроизвёл исполнением/чтением ДО правки.
Итог: **все находки подтверждены реальными**, ложных нет. Ниже — воспроизведение и фикс.

## Д3 — `mathlib/calibration/conditional.py`, `mathlib/cascade.py`, `ops/calibrate_conditional.py`

| # | Находка | Вердикт | Что сделано |
|---|---|---|---|
| 1 | HIGH: `cascade_from_quotes(lookback=400)` < TRAIN+TEST=756 → условный всегда «нет данных» | ПОДТВЕРЖДЕНА | `node_cascade` получил отдельные `cond_source_ret/cond_node_ret`; `cascade_from_quotes` при `with_conditional` берёт срез `max(lookback, TRAIN+TEST+1)`. Безусловная бета — прежний lookback 400 байт-в-байт. Тест `test_cascade_from_quotes_conditional_measures_on_long_history` (10 лет → измерение состоялось, не «история N<train+test»). |
| 2 | HIGH: OOS-фолд пускает эпизод, у которого окно шока частично в train | ПОДТВЕРЖДЕНА | Фильтр эпизодов теперь требует ВСЁ окно шока `[t−W+1..t]` И весь отклик `t+max_lag` внутри среза (и для train, и для OOS). Тест `test_walkforward_oos_purity_no_train_bleed` (эпизод у `test_start` в OOS не попадает). |
| 3 | HIGH: секция «Устойчивость к порогу» подаёт неустойчивые пары (SPY→DBC, XOM→EEM «установлено» только на 0.6σ) как норму | ПОДТВЕРЖДЕНА | Секция переименована в «Проверка устойчивости…»; вердикт каждой пары помечается флагом; пары, где статус меняется поперёк 0.4/0.5/0.6σ → `⚠ НЕУСТОЙЧИВО (контрсвидетельство)`; сводка неустойчивых; итог — по каноническому 0.5σ. |
| 4 | LOW: `effect_stats` сравнивает знак-выровненный эпизод с сырой baseline | ПОДТВЕРЖДЕНА | Baseline проецируется на средний знак шоков (`mean_baseline·s̄`) — дрейф цели вычитается симметрично. Поля переименованы: `mean_baseline_raw/mean_baseline_signaligned/mean_shock_sign`. Тест `test_effect_baseline_sign_aligned_no_drift_artifact`. |
| 5 | LOW: ярус A/B считает N по всем валидным OOS-фолдам, вкл. неподтвердившие | ПОДТВЕРЖДЕНА | `n_episodes_oos` = сумма эпизодов ТОЛЬКО подтвердивших фолдов (маппинг ярусов по ним); диагностика всех валидных — в `n_episodes_oos_valid`. Тест `test_n_oos_counts_only_confirming_folds`. |
| 6а | LOW: rename `cohen_d→glass_delta`, `gain_ci95→gain_ci95_fullsample` | ПОДТВЕРЖДЕНА | Переименовано в коде, отчёте и перегенерённом YAML (Glass Δ — знаменатель σ baseline, не пул). |
| 6б | LOW: декларация condition-on-success смещения gain | ПОДТВЕРЖДЕНА | Добавлены `gain_bias_note` и явная пометка в провенансе (gain — медиана OOS ПОДТВЕРДИВШИХ, оптимистично смещена). |
| 6в | LOW: страж-тест отравления модуля conditional | ПОДТВЕРЖДЕНА | `test_conditional_module_poisoning_does_not_affect_default_cascade` (monkeypatch.setattr на `mathlib.calibration.conditional`). |
| 6г | LOW: ключ `sensitivity_conditional` и в ветке «нет данных» | ПОДТВЕРЖДЕНА | `node_cascade` при `with_conditional=True` присоединяет запись и на отказном пути. Тест `test_no_data_branch_carries_conditional_key_when_requested`. |
| 7 | Перегенерация отчёта/YAML + контроли | — | `ops/calibrate_conditional.py` прогнан на read-only снапшоте боевых `quotes`. Контроли: **USO→BNO — установлено, ярус A** (сильная связь цела); XOM→EEM, BNO→FRO/USO→DHT и др. — честное «не установлено» (XOM→EEM помечена НЕУСТОЙЧИВО). |

## Э4 — orchestrator/{world_map,segment_screen,world_enum,world_enum_dryrun,edge_forward}.py, ops/{promote_edges,rescan_maps}.py

| # | Находка | Вердикт | Что сделано |
|---|---|---|---|
| 8 | BLOCKER: кандидат-рёбра при кэпе 40 вытесняют библиотечные (сорт алфавитный) | ПОДТВЕРЖДЕНА | `edge_library` сортирует библиотеку первой (`origin!='library'` в ключе); суб-кэп кандидатов `≤ MAX_SEALS//2`. Кэп 40 не поднят. Тесты `test_library_edges_printed_first_under_cap` (30+30→все 30 библ), `test_candidate_subcap_limits_candidate_seals`. |
| 9 | BLOCKER: `world_enum_dryrun --append-candidates` без `--candidates-path` пишет в боевой реестр | ПОДТВЕРЖДЕНА | `main()` получил `--candidates-path`; `--append-candidates` без него → `ap.error` (SystemExit). Тест `test_main_append_candidates_requires_explicit_candidates_path`. |
| 10 | BLOCKER: валидация карты пропускает тикеры/числа в строковых полях | ПОДТВЕРЖДЕНА | Рекурсивный `_scan_leaks`: тикеро-подобные (`$X`, `X.US`) и любые цифры в ЛЮБОЙ строке сегмента + верхний уровень событие/обоснование → отказ (кроме структурного «порядок»). Тесты на контрпример отчёта (верхний уровень и глубина сегмента) + регрессия чистой карты. |
| 11 | HIGH: квотные ошибки EODHD в теле HTTP-200 маскируются под пустой скрин | ПОДТВЕРЖДЕНА | `fetch_screener_page` детектит `error/message` с quota/payment/limit/402/429 в теле → `QuotaError` → алерт в notices + фолбэк. Тест `test_quota_error_in_http200_body_alerts_and_falls_back`. |
| 12 | HIGH: дедуп эпизода по `sealed_at` склеивает независимые legacy-печати | ПОДТВЕРЖДЕНА | `_episode` = ТОЛЬКО явное поле `episode` (без `sealed_at`-прокси); legacy без episode проходят раздельно. Обновлён тест `test_episode_dedup_legacy_without_episode_not_merged` (2 строки, 0 дублей). |
| 13 | medium: «индустрии» карты не валидируются → пустой скрин при опечатке | ПОДТВЕРЖДЕНА | Деградация уровня industry→sector в screener и в БД-фолбэке, честная пометка «деградация» в источнике. Тесты `test_industry_typo_degrades_to_sector_screener/_db`. |
| 14 | medium: квота сегментов статична (комментарий врёт) | ПОДТВЕРЖДЕНА | Динамическая квота `ceil(остаток/оставшиеся_сегменты)` на сегмент + 2-й проход по срезанным квотой при итоге < target_min. Комментарий=код. Тест `test_dynamic_quota_and_second_pass_reaches_target_min` (5 узких+1 широкий→≥100). |
| 15 | medium: `rescan_maps` дифф несравним (кэп 300, источник не исключён) | ПОДТВЕРЖДЕНА | Воспроизводит правила `enumerate_event`: та же динамическая квота + исключение источника шока. Тест `test_rescan_excludes_shock_source_and_applies_quota`. |
| 16 | medium: бюджет-гард Инв#5 (`precheck_or_raise("world_map")`) мёртв | ПОДТВЕРЖДЕНА | `build_world_map` вызывает `RB.precheck_or_raise("world_map")` перед LLM (реальные потолки production, суб-потолок `world_map: 3.00` уже в limits). Тест `test_world_map_precheck_refusal_propagates`. |
| 17 | Герметичность: тесты реестра — tmp, дефолт боевого пути цел | ПОДТВЕРЖДЕНА | Все тесты кандидатов пишут в tmp; добавлен `test_candidate_registry_default_paths_are_repo_files` (дефолт `knowledge/edge_candidates.jsonl` цел, общий у world_enum/edge_forward). |

Ложных находок нет — все 17 пунктов реальны, каждый закрыт фиксом + regression-тестом (либо
перегенерацией отчёта для п.7).
