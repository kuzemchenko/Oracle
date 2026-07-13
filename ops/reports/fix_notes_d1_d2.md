# Fix-notes: закрытие блокеров/HIGH ревью этапов Д1 и Д2

Метод: каждую находку сперва ВЕРИФИЦИРУЮ (ревью может ошибаться); реальную — чиню +
regression-тест; ложную — опровергаю исполнением здесь. Боевой `config/thresholds.yaml`
НЕ трогаю (перегенерацию вернёт основная сессия при закрытии гейта se-d1). Два теста
`test_live_thresholds_yaml_consistent_with_base` / `test_live_config_has_tail_df_section`
остаются в skip (Д1 деактивирован в бою до гейта). Драйверы гоняю в tmp/отчётные пути.

Источники находок: `reports/cross_review_20260713T124030Z.md` (Д1, gpt-5.5),
`reports/cross_review_20260713T124013Z.md` (Д2, gpt-5.5); плюс дополнительные пункты
задания (9–11 Д1). Боевая БД читается read-only.

---

## Д1

### 3 [HIGH] OOS-валидация df_med не walk-forward-чистая — ПОДТВЕРЖДЕНО, исправлено
`mathlib/calibration/tail_df.py:calibrate_instrument`. Прежняя v2 считала ГЛОБАЛЬНУЮ
медиану df по ВСЕМ фолдам и валидировала её на test-окне каждого фолда — в OOS-оценку
ранних фолдов затекала информация из будущих train-фолдов. Фикс: фолд i валидируется
против РАСШИРЯЮЩЕЙСЯ медианы `median(dfs[:i+1])` (только прошлые+текущий train);
значение в конфиг — по-прежнему полносэмпловая медиана (легально для форварда).
Regression: `test_calibrate_oos_validation_is_walk_forward_clean`.

### 7 [HIGH] pooled_fallback_df: поле `walkforward_check` лживо — ПОДТВЕРЖДЕНО, исправлено
`mathlib/calibration/tail_df.py:pooled_fallback_df`. Пул — КОНКАТЕНАЦИЯ разных
инструментов; фолды calibrate_instrument идут по границам инструментов, не по времени —
это не walk-forward. Честно переименовано в `pool_self_consistency` (+пометка «не WF»).
Значение фолбэка (fit_df по пулу) не меняется. Regression:
`test_pooled_fallback_reports_value_and_check` (обновлён).

### 5 [HIGH] округление p до BH — ПОДТВЕРЖДЕНО, исправлено (боевой event_scan)
`orchestrator/event_scan.py`. `price_vol_signals`/`trend_signals` писали `round(p,4)` и
`scan_events` кормил BH этими округлёнными значениями — на больших m это меняет набор
открытий (0.000049→0.0000 ложно проходит; 0.05004→0.05 ложно проходит). Фикс: каждый
статистический сигнал несёт `_p_raw` (полная точность) — BH считается по нему, `p_value`
(округлён) остаётся только для протокола; `_p_raw` вычищается перед выдачей (протокол
байт-в-байт прежний). Regression: `test_bh_runs_on_unrounded_pvalues`,
`test_bh_rounding_would_flip_decision` (прямой контрпример: raw 0.05004 не проходит,
округлённый 0.05 прошёл бы). NB: фикс улучшает ТЕКУЩИЙ живой скан.

### 9 [LOW] _resolve_df пропускает bool — ПОДТВЕРЖДЕНО, исправлено
`orchestrator/event_scan.py:_resolve_df`. `isinstance(v,(int,float)) and v>0` пропускал
`True` (bool ⊂ int) → df=1.0. Добавлено `and not isinstance(v, bool)` на per_instrument и
фолбэк. Regression: `test_resolve_df_ignores_bool`.

### 8 [HIGH, боевая] volume=0 артефакты + гейт давности бара — ПОДТВЕРЖДЕНО, исправлено
Логика жила только в диагностике replay (`_annotate`), а САМ боевой скан пропустил бы
артефакт. Перенесено в боевой путь:
- `orchestrator/context.py:_indicators` — последний бар с `volume<=0`/None (битая строка
  фида) даёт log(max(0,1))=0 → ложный объёмный z; теперь `vol_z_20`/`vol_z_log_20`=None +
  `vol_data_note` (П8). event_scan такой инструмент по объёму пропустит (isinstance-гейт),
  ценовая метрика не тронута.
- `orchestrator/event_scan.py:scan_events` — новый `asof_date` (по умолчанию None = прежнее
  поведение, байт-в-байт): при задании инструменты с последним баром старше
  `MAX_BAR_AGE_DAYS=7` исключаются из ценового скана (delisted/пропал фид), перечислены в
  `протухшие_бары` (П8). `scan_events_live` передаёт `datetime.date.today()`.
Regression: `test_indicators_zero_volume_last_bar_nulls_vol_metrics`,
`test_scan_staleness_gate_drops_stale_bar`. КЛЮЧЕВОЙ фикс — позволяет основной сессии
безопасно ре-активировать Д1.
