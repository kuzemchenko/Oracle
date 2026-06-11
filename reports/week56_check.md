# Проверка Недель 5–6 — итоги перед gate-check

Дата: 2026-06-11 · режим прогона-источника: **live OpenRouter** (`funnel_20260611T135205Z`).
Сводка по пяти пунктам. **Открыт блокер по п.2 (разметка волн).**

---

## 1. Агенты в `agents/prompts/` + подтверждение П8 в каждом

21 файл: блоки **B(10) + C(4) + D(2) + G(5)**.

```
b_behavioral_economist.md   b_causal_links.md         b_cyclist.md
b_elliott_wave.md           b_fundamental.md          b_game_theory.md
b_historian_events.md       b_historian_precursors.md b_omens.md
b_technical.md              c_adjacent_domains.md     c_cascades.md
c_context_filter.md         c_non_obviousness.md      d_anti_manipulation.md
d_timeliness.md             g_credibility.md          g_outcome_analyst.md
g_predictions_journalist.md g_validator.md            g_weight_calibrator.md
```

**grep П8** (формулировка «нет данных — обязательный и поощряемый»):
```
$ grep -lE "ОБЯЗАТЕЛЬНЫЙ и ПООЩРЯЕМЫЙ" agents/prompts/*.md | wc -l
21
$ grep -c "НУЛЕВЫЕ ВЫДУМКИ" agents/prompts/*.md | grep -v ":1$"
(пусто — везде ровно один блок П8)
```
**Итог: 21/21 промптов содержат требование П8.** ✅

---

## 2. Разметка волн Эллиотта в mathlib — ✅ РЕАЛИЗОВАНА (блокер закрыт)

`mathlib/waves.py` (чистый numpy, без LLM):
- `zigzag_pivots(prices, threshold_pct)` — пивоты по фактическим экстремумам; последний помечен
  `confirmed=False` (незавершённая волна, честно);
- `label_impulse(6 пивотов)` — проверка **трёх жёстких правил Эллиотта**: R1 (волна 2 не за начало
  волны 1), R2 (волна 3 не самая короткая), R3 (волна 4 не перекрывает волну 1) + длины и фибо-отношения;
- `label_correction(4 пивота)` — измерения ABC; `fib_retracement`, `nearest_fib`;
- `wave_markup(...)` — полная числовая разметка для агента; счёт НЕ выдаётся как «единственно верный»
  (П8 — неоднозначность счёта явно помечена, альтернатива за агентом).

```
$ python -c "import mathlib.waves as W; print([n for n in dir(W) if not n.startswith('_')])"
['FIB_LEVELS','fib_retracement','label_correction','label_impulse','nearest_fib',
 'np','wave_markup','zigzag_pivots']

$ python -m pytest mathlib/tests/test_waves.py -q
16 passed
```
**Тестов по волнам: 16, все зелёные** (валидный импульс up/down; каждое нарушение R1/R2/R3 по
отдельности; пивоты на сконструированном зигзаге; фибо; недостаток данных). Всего в репо **166** тестов.

**Подключено к волновику** (`orchestrator/context.py`): срез `b_elliott_wave` получает `waves.{symbol}`.
LIVE-проверка (`x-ai/grok-4.3`): волновик ИНТЕРПРЕТИРУЕТ разметку — основания ссылаются на
`waves.BNO.US.impulse_last` / `waves.USO.US.impulse_last`, видит нарушения R3/R1 и честно выдаёт
«нет данных» (валидного счёта сейчас нет), П8-чисто. То есть теперь это его суждение по тулкиту,
а не следствие отсутствия тулкита. Порядок сборки восстановлен (mathlib перед агентом).

---

## 3. Поле суждений 3–4 школ из сквозного прогона (стандартный формат Дирижёра §5.2)

Из `journal/funnel_logs/funnel_20260611T135205Z.json`. Каждое суждение содержит пять полей §5.2:
**вывод + вероятность + уверенность + данные-основания + что неизвестно** (+ кандидаты у школ).

### b_causal_links — Агент жизненных взаимосвязей · `anthropic/claude-opus-4.8`
- **вывод:** «Геополитическое событие — удары США по целям в Иране и атака на торговые суда/морской
  коридор — активирует причинную цепочку премии за риск предложения нефти»
- **вероятность:** 0.58 · **уверенность:** низкая
- **данные-основания:**
  - {факт: «Новость об ударах США по целям в Иране», источник: `news (gdelt, 2026-06-11)`}
  - {факт: «Атака на торговые суда/морской коридор (осуждение в ООН)», источник: `news (gdelt, 2026-06-11)`}
  - {факт: «Эмп. связь нефтекомплекса USO↔BNO синхронна и сильна», источник: `causal_links pair [USO.US,BNO.US]`}
- **что неизвестно:** «связь ‘геошок на Ближнем Востоке → Brent’ ОТСУТСТВУЕТ в causal_links — лаг и
  сила не измерены»; «все эмпирические лаги в библиотеке = 0»
- **кандидат:** BNO.US / **лонг** / премия за риск предложения переносится на Brent-прокси

### b_technical — Технический аналитик · `google/gemini-3.1-pro-preview`
- **вывод:** «Нефтяные ETF (BNO, USO) в нисходящем тренде ниже ключевых SMA без перепроданности»
- **вероятность:** 0.65 · **уверенность:** средняя
- **данные-основания:** {источник: `indicators.BNO.US` — цена ниже SMA20/SMA50; MACD-гистограмма < 0; RSI не перепродан}
- **что неизвестно:** «открытый интерес (OI) — нет в фиде»; «IV опционов — не подключена»
- **кандидат:** BNO.US / **шорт** / продолжение нисходящего движения

### b_game_theory — Теоретик игр · `deepseek/deepseek-r1`
- **вывод:** «Геополитическая эскалация США–Иран создаёт риск премии за поставки нефти»
- **вероятность:** 0.70 · **уверенность:** средняя
- **данные-основания:** {источник: `news[6]` — второй день ударов}; {источник: `news[8]` — прогноз длительности конфликта}
- **что неизвестно:** «объёмы экспортных поставок Ирана»; «позиционирование хедж-фондов в энергетике»
- **кандидат:** BNO.US / **лонг** / премия через угрозу перебоев поставок

### b_omens — Агент примет (КАРАНТИН) · `google/gemini-3.5-flash`
- **вывод:** «Неподтверждённая примета: удары США + атаки на суда → премия за геориск»
- **вероятность:** 0.55 · **уверенность:** низкая
- **что неизвестно:** «статистическое подтверждение приметы на истории (карантинный режим)»

> **Наблюдение Дирижёра:** технарь даёт **шорт**, остальные — **лонг** по тому же BNO.US →
> карта противоречий §5.4 зафиксировала расхождение, уверенность итога понижена.

---

## 4. Бюджет: `journal/costs.jsonl` (последние 10) + `budget.py`

```
{"ts":"2026-06-11T13:55:53Z","mode":"live","agent":"c_adjacent_domains","model":"anthropic/claude-sonnet-4.6","prompt_tokens":6726,"completion_tokens":1577,"cost_usd":0.043833,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:56:25Z","mode":"live","agent":"c_non_obviousness","model":"openai/gpt-5.5","prompt_tokens":2226,"completion_tokens":1723,"cost_usd":0.06282,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:56:33Z","mode":"live","agent":"c_context_filter","model":"anthropic/claude-haiku-4.5","prompt_tokens":2002,"completion_tokens":1026,"cost_usd":0.007132,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:57:18Z","mode":"live","agent":"d_timeliness","model":"anthropic/claude-sonnet-4.6","prompt_tokens":4682,"completion_tokens":2283,"cost_usd":0.048291,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:57:46Z","mode":"live","agent":"d_anti_manipulation","model":"openai/gpt-5.5","prompt_tokens":3992,"completion_tokens":1356,"cost_usd":0.06064,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:58:10Z","mode":"live","agent":"g_validator","model":"openai/gpt-5.5","prompt_tokens":2242,"completion_tokens":1502,"cost_usd":0.05627,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:58:44Z","mode":"live","agent":"g_predictions_journalist","model":"anthropic/claude-sonnet-4.6","prompt_tokens":2859,"completion_tokens":1907,"cost_usd":0.037182,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:59:01Z","mode":"live","agent":"g_outcome_analyst","model":"anthropic/claude-opus-4.8","prompt_tokens":3155,"completion_tokens":1072,"cost_usd":0.042575,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:59:17Z","mode":"live","agent":"g_weight_calibrator","model":"anthropic/claude-haiku-4.5","prompt_tokens":2822,"completion_tokens":1805,"cost_usd":0.011847,"ok":true,"run_id":"funnel_20260611T135205Z"}
{"ts":"2026-06-11T13:59:39Z","mode":"live","agent":"g_credibility","model":"openai/gpt-5.5","prompt_tokens":2201,"completion_tokens":1424,"cost_usd":0.053725,"ok":true,"run_id":"funnel_20260611T135205Z"}
```
```
$ python3 ops/budget.py
[бюджет] ВНИМАНИЕ ≥80%: токены $459.97/$500 (92%), всего ~$660/$700
```
Полный live-прогон 21 агента = **$0.78** (локальная сумма всех 23 строк журнала = $0.82),
в пределах `per_run_token_budget_usd.funnel_full=$8.00`.

**⚠️ Два флага бюджета (требуют решения пользователя):**
- **A. Месяц на 92%.** `/api/v1/key`: `usage_monthly=459.97` против $500 (§30) → статус ВНИМАНИЕ
  (инвариант 5). До новых live-прогонов — на контроль.
- **B. Расхождение.** Локальный журнал за всё = $0.82, но ключ показывает `usage_daily=62.53` за
  сегодня. Наши прогоны столько не стоили → ключом сегодня пользовались помимо этой сессии, либо
  есть трата вне `costs.jsonl`. Источник $62/день нужно установить.
- *Мелочь:* `budget.py` хардкодит $500, а жёсткий лимит ключа — `limit=300`, сброс **дневной**
  (`limit_remaining=237.47`). Свести с `config/limits.yaml`.

---

## 5. Фолбеки и запиненные версии в `models.yaml`

**Фолбеки:** в live-прогоне ни одного фолбека не было — все primary ответили (файлов
`journal/funnel_logs/*model_swaps*` нет). Логика перехода и запись смены модели покрыты юнит-тестом:
```
$ python -m pytest orchestrator/tests/test_smoke_funnel.py -k fallback -q
1 passed, 16 deselected
```
(`test_fallback_chain_switches_and_logs`: primary→503→уход на фолбек, `fallback_index==1`,
запись в `*_model_swaps.jsonl`). Хелпер П10 `judge_family_ok` реализован.

**Пиннинг:**
```
pinned_quarter: "2026-Q2"
catalog_synced:  "2026-06-11"
```
Все ID версионированы: `anthropic/claude-{opus-4.8,sonnet-4.6,haiku-4.5}`, `openai/gpt-{5.5,5.1,5-mini}`,
`google/gemini-{3.1-pro-preview,2.5-pro,3.5-flash}`, `x-ai/grok-4.3`, `deepseek/{deepseek-r1,deepseek-chat-v3.1}`,
`qwen/qwen3-max`, `moonshotai/kimi-k2.5`. Обновление — ежеквартально с regression на masked_cases (§25).

---

## Вывод по гейту

| Пункт | Статус |
|---|---|
| 1. Промпты B/C/D/G + П8 | ✅ 21/21 |
| 2. Разметка волн Эллиотта в mathlib | ✅ реализована (mathlib/waves.py, 16 тестов, подключена к волновику) |
| 3. Поле суждений школ в формате §5.2 | ✅ live-прогон |
| 4. Бюджет/журнал/панель | ✅ работает · ⚠️ флаги A/B |
| 5. Фолбеки + пиннинг | ✅ тест + версии |

**Блокер (2) закрыт.** Технических препятствий к gate-check Нед.5–6 не осталось. Остаётся
**не-блокирующий** вопрос — флаг B бюджета (источник $62/день на ключе): решение пользователя,
на сам гейт Нед.5–6 не влияет. Жду ок на запуск gate-check.
