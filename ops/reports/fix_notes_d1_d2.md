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
