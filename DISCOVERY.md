# DISCOVERY.md — карта системы «Оракул»

> Разведка в режиме **только чтение**, дата: 2026-06-19. Сервер: `ubuntu-4gb-hel1-1`.
> Секреты не печатались — указаны только пути/имена переменных.
> Источник истины по дизайну — `spec/MASTER_SPEC.md` (канон) и `CLAUDE.md` (инварианты).

---

## 0. TL;DR (что это и в каком оно состоянии)

«Оракул» — мультиагентная система инвестиционного анализа на чистом Python (venv, **без Docker**).
Запускается двумя способами: **(а)** демон Telegram-пульта `oracle-bot.service` (systemd, работает) и
**(б)** пакет задач по **cron** (сбор данных, котировки, калибровка, тематический Brent, сверка исходов,
event-first воронка, бюджет). Веб-портов Оракул не слушает — бот работает на исходящем long-polling.

Стадия разработки: идёт перестройка воронки `event → cascade → instrument` (cascade-first, см.
`spec/PLAN_cascade_first.md`), в дереве **23 незакоммиченных файла** (Этап ~7). Денежные ворота:
пройден Gate С→Б; до ворот Б→Д нужно 270 разрешённых исходов — сейчас **58 запечатанных прогнозов,
0 сверенных исходов**.

---

## 1. КОД И РЕПО

### 1.1 Дерево (верхний уровень)
```
/root/oracle
├── agents/         registry.py + build_prompts.py + prompts/   (21 промпт, генерируются кодом)
├── orchestrator/   дирижёр, воронка, контуры (см. §3)
├── mathlib/        детерминированная математика (Brier, FDR, Kelly, cascade, scoring, sealing…)
├── data/           ingestion: gdelt.py, newsapi_ai.py, trends.py, eodhd.py, news_ingest.py
├── config/         *.yaml (модели, лимиты, веса, пороги, вселенная, новости…)
├── knowledge/      каскадные цепочки/лаги/чувствительности, причинно-следственные связи, masked_cases/
├── journal/        ВЫХОД и состояние: *.jsonl (запечатанные), funnel_logs/, bot_state.json, *.log
├── storage/        oracle.db (sqlite, ~66 МБ; в .gitignore)
├── ops/            бот (bot*.py), calibrate_*.py, budget.py, crontab.txt, oracle-bot.service
├── dashboard/      build_dashboard.py → *.html (в .gitignore)
├── reports/        аудиты, deep-research, «как устроен»
├── spec/           MASTER_SPEC.md (канон) + PLAN_cascade_first.md + ARCHITECTURE_PLAIN.md + PRESENTATION.md
├── .venv/          окружение (Python 3, см. ниже)
├── .env            КЛЮЧИ (см. §4)
├── CLAUDE.md       инварианты проекта (override-инструкции)
└── .claude/        hooks/ (guard_journal.py — защита запечатанных журналов), skills/, settings*.json
```

### 1.2 Точки входа (CLI, `__main__`)
| Скрипт | Назначение | Режимы / флаги |
|---|---|---|
| `orchestrator/run.py` | **главный дирижёр-диспетчер** | `--mode {auto,live,mock,funnel,theme,multi,event_first,masked,ablation,resolve,calibrate,…}`; `--k`, `--theme/--asset`, `--agents`, `--doubt`, `--from-run`, `--no-write` |
| `orchestrator/event_first.py` | event-first контур end-to-end (скан→каскад→инструмент) | `--mode {mock,live,auto}`, `--k`, `--research-only` |
| `orchestrator/event_first_dryrun.py` | сухой прогон event-first рядом с боевым | — |
| `data/news_ingest.py` | единый суточный сбор новостей (GDELT+NewsAPI.ai+pytrends) | — |
| `data/gdelt.py` / `data/newsapi_ai.py` / `data/trends.py` / `data/eodhd.py` | по-источниковая загрузка | `--status`, `--extras`, `--options`, `--symbol`, `--full` |
| `ops/bot.py` | Telegram-пульт (long-polling, демон) | `--daemon`, `--no-token-warn` |
| `ops/budget.py` | панель бюджета §15 (exit code 3 = превышение) | — |
| `ops/calibrate_*.py` | калибровка весов/порогов/лагов/чувствительностей (пишут конфиги) | — |
| `agents/build_prompts.py` | регенерация всех промптов (руками промпты не править) | `--check` |
| `dashboard/build_dashboard.py` | сборка HTML-дашборда | — |

### 1.3 Манифесты зависимостей
- **Стандартного манифеста НЕТ**: нет `requirements.txt`, `pyproject.toml`, `setup.py`, `Pipfile`.
- Фактические зависимости — только в `.venv` (`pip freeze`): `numpy 2.4.6`, `pandas 3.0.3`,
  `scipy 1.17.1`, `requests 2.34.2`, `PyYAML 6.0.3`, `pytrends 4.9.2`, `lxml`, `python-dateutil`,
  `pytest 9.0.3`. Всё остальное — стандартная библиотека (`sqlite3`, `json`, `hashlib`, `threading`, `urllib`).
- ⚠️ Воспроизводимость окружения нигде не зафиксирована (см. РИСКИ).

### 1.4 Документация
- `CLAUDE.md` — инварианты П8/П10/П16, порядок сборки, денежные ворота, стиль работы.
- `spec/MASTER_SPEC.md` — полная каноническая спецификация (§1–30).
- `spec/PLAN_cascade_first.md` — план перестройки воронки (Этапы 0–7).
- `spec/ARCHITECTURE_PLAIN.md`, `spec/PRESENTATION.md` — обзор и формат отчёта (13 полей §8).
- `README_КОМАНДЫ.md`, `КОМПЛЕКТ_starter_kit_v3.md`, `ops/BOT_README.md`, `data/README.md`, `ops/goals.md`.
- `reports/ОРАКУЛ_как_устроен.md` — система «как построена».

---

## 2. ЧТО РЕАЛЬНО РАБОТАЕТ

### 2.1 Процессы Оракула
- **Единственный процесс Оракула:** `oracle-bot.service` → `/root/oracle/.venv/bin/python /root/oracle/ops/bot.py` (PID 816914, поднят 2026-06-18 14:55, ~32 МБ).
- Остальные python/docker-процессы на машине — **ЧУЖИЕ проекты** (mycode/relcode/reelsforge/readai): gunicorn/uvicorn/celery, streamlit, телеграм-боты. К Оракулу отношения не имеют.

### 2.2 systemd
- `oracle-bot.service` — **enabled + active (running)**. Юнит-файл: `/etc/systemd/system/oracle-bot.service` (копия в репо `ops/oracle-bot.service`).
- Прочие активные боты (`reelsforge-bot`, `relcode-bot`) — чужие.

### 2.3 Docker / compose
- Оракул **Docker не использует**, compose-файлов в репо нет.
- Запущенные контейнеры (`mycode-*`, `relcode-db`, `pgvector`, `redis`) принадлежат другим проектам.

### 2.4 Порты
- **Оракул не слушает ни одного порта** — Telegram-бот работает на исходящем long-polling.
- Все LISTEN-порты (`:80/:8443/:443` nginx, `:8000/:8001/:5433` docker-proxy чужих сервисов, `:3306/:33060` mysql, `:8501` streamlit, `:3100` node, `:631` cups, `:22` ssh, tailscale, `:18789` openclaw, `:8901`) — **не Оракул**.

### 2.5 Расписание (cron) — ⚠️ установленное ≠ репозиторий
Установленный пользовательский crontab (root) поднимает окружение в каждой строке
(`cd /root/oracle && set -a && . ./.env && set +a && .venv/bin/python …`):

| Время (UTC) | Что запускается |
|---|---|
| `45 6 * * *` | `data/news_ingest.py` — суточный сбор новостей |
| `50 6 * * *` | `data/eodhd.py --extras --options` — котировки + Tier 0 + опционы |
| `0 7 * * *` | `ops/budget.py` — панель бюджета |
| `0 8 * * 2,5` | `run.py --mode calibrate` — калибровочные прогнозы (вт, пт) |
| `30 7 * * 1-5` | `run.py --mode theme --asset brent` — Тема №1 Brent (будни) |
| `0 21 * * *` | `run.py --mode resolve` — сверка исходов |
| **`0 9 * * 1`** | `run.py --mode event_first --k 2` — **event-first воронка (ТОЛЬКО ПОНЕДЕЛЬНИК)** |
| `0 18 * * 5` | напоминание `/review-week` в reminders.log |

> **Расхождение:** репозиторный `ops/crontab.txt` уже переведён на `0 9 * * 1-5` (event-first **каждый будний день**),
> но в установленный crontab это **НЕ применено** — живёт старое «только понедельник». Единственная отличающаяся строка.

### 2.6 systemd-таймеры
- Оракульских таймеров нет — только системные (sysstat, logrotate, certbot, apt-daily и т.п.).

### 2.7 Свежие логи (без секретов)
- `journal/cron.log` (последний прогон):
  - суточный сбор работает: **8985 новостей / 7675 уникальных**, источники `gdelt 5992 + newsapi_ai 2993`, Trends 675 точек; Gate Нед.2 ✅.
  - `funnel_20260618T073001Z` (live, brent): 21/21 агентов ок, 6 кандидатов → **«стоящих идей нет»** (скан 26→FDR 0 → фильтр 1 → дебаты 1 → устояло 0).
  - `resolve`: **в журнале 58 прогнозов, сверено 0, pending 58; до ворот 270 — 270**; Brier=None (исходов ещё нет).
- `journal/costs.jsonl`: последняя запись `2026-06-18T23:42` — live `challenge`, `e_judge` = `google/gemini-3.1-pro-preview`, $0.0666. Всего 1125 строк.

---

## 3. ПОТОК ДАННЫХ (end-to-end)

### 3.1 Откуда поступают данные
| Источник | Файл | Ключ (где, НЕ значение) | Бесплатно? |
|---|---|---|---|
| **GDELT DOC 2.0** (новости, мультиязык) | `data/gdelt.py` | нет ключа; backoff на 429/5xx | да |
| **NewsAPI.ai / EventRegistry** | `data/newsapi_ai.py` | env `NEWSAPI_AI_KEY` (из `.env`) | платно |
| **Google Trends (pytrends)** | `data/trends.py` | нет ключа | да |
| **EODHD** (котировки/фундаментал/инсайдеры/опционы) | `data/eodhd.py` | env `EODHD_API_KEY` (из `.env`) | платно (Tier 0) |
| нормализация/дедуп/теги | `data/news_ingest.py`, `data/news_common.py`, `data/_maps.py` | — | — |

### 3.2 Стадии обработки (где определены)
```
news_ingest.py / eodhd.py
        ↓ (запись)
storage/oracle.db  (таблицы: news, quotes, trends, trends_related, fundamentals,
                    insider_tx, earnings_calendar, options_summary)
        ↓
orchestrator/event_scan.py     — открытый скан: кластеры новостей (multi_event.py),
                                 тренд-сигналы, ценовые z-аномалии, FDR (mathlib/fdr.py)
        ↓ (шоки/события)
orchestrator/cascade_build.py + mathlib/cascade.py
                               — детерминированная сборка цепочек: ярусы A/B/C (порядки 1–4),
                                 амплитуда = ист.чувствительность (knowledge/cascade_sensitivities.yaml),
                                 лаг (knowledge/cascade_lags_calibrated.yaml), цепочки (knowledge/cascade_chains.yaml)
        ↓
orchestrator/funnel.py         — НЕопиньонированный дирижёр §5: поле суждений 21 агента
   ├─ agents/registry.py       — реестр агентов (блоки B/C/D/G), промпты agents/prompts/*.md
   └─ orchestrator/openrouter.py — маршрутизация роль→модель→семейство, фолбэки §26
        ↓
orchestrator/debate.py         — состязательный контур (блок E): генератор / критик / адвокат /
                                 data_reviewer / СЛЕПОЙ судья (П10: разные семейства, рандом порядка, рубрика)
        ↓
mathlib/scoring.py             — скоринг §7 (веса config/weights.yaml 22/22/18/14/14/10, рубрика config/rubric.yaml)
        ↓
orchestrator/synthesis.py      — синтез §8 + mathlib/portfolio.py + mathlib/kelly.py;
   risk-агент (хедж) + synthesizer (13 полей §8) → топ-3
        ↓
orchestrator/cascade_resolve.py — узел → запечатать (mathlib/sealing.py) ИЛИ лист ожидания
        ↓
journal/* (см. §3.4) + Telegram (ops/bot.py)
```
- Брандмауэр вселенной: `orchestrator/universe_resolver.py` (`is_sealable`) + `config/universe.yaml` — что вообще торгуемо.
- Контрфактический протокол для абляции пишется на каждом синтезе (§11.1) → `orchestrator/ablation.py`.

### 3.3 Модели / провайдер
- Единственный провайдер — **OpenRouter** (`orchestrator/openrouter.py`, `LiveClient`/`MockClient`).
- Ключ: env **`OPENROUTER_API_KEY`** (из `.env`).
- Назначение роль→модель→семейство: `config/models.yaml` (pinned-квартал, каталог синхронизирован 2026-06-11).
  Семейства: anthropic / openai / google / x-ai / deepseek / qwen. **П10**: генератор, критик, судья — РАЗНЫЕ семейства; фолбэк-судья никогда не из семейства генератора.
- Каждый своп модели логируется в `journal/funnel_logs/{run_id}_model_swaps.jsonl`.

### 3.4 Куда уходит результат (sinks)
| Sink | Файл-писатель | Содержимое |
|---|---|---|
| `journal/predictions.jsonl` | через `mathlib.seal()` (защищён хуком) | запечатанные прогнозы §9/П16 (ts+hash) |
| `journal/costs.jsonl` | `orchestrator/openrouter.py` | трата по каждому LLM-вызову |
| `journal/watchlist.jsonl` | `orchestrator/cascade_resolve.py` | узлы каскада в листе ожидания §17 |
| `journal/funnel_logs/{run_id}.{json,md}` | `funnel.py`/`event_first.py` | полное поле суждений + протокол |
| `journal/bot_state.json` | `ops/bot_state.py` | мутабельное состояние бота (дедуп пушей/решений) |
| `journal/challenges/*.jsonl` | `orchestrator/challenge.py` | точечный состязательный разбор (§4 блок E) |
| `journal/proposed_adjustments.md` | `orchestrator/ablation.py` | предложения поправок |
| Telegram | `ops/bot.py` + `ops/bot_reports.py` | отчёт 13 полей §8, кнопки решений §12 (24ч-пауза), алерты бюджета |
| `dashboard/*.html` | `dashboard/build_dashboard.py` | дашборд метрик §15 |

---

## 4. СОСТОЯНИЕ И КОНФИГ

### 4.1 Хранилище состояния
- **БД:** `storage/oracle.db` (**sqlite3**, ~66 МБ, в `.gitignore` — перекачиваемый кэш).
  Таблицы: `news, quotes, trends, trends_related, fundamentals, insider_tx, earnings_calendar, options_summary`.
- **Запечатанные журналы** (в git, неизменяемые): `journal/predictions.jsonl`, `journal/watchlist.jsonl`, `journal/funnel_logs/`.
- **Мутабельное состояние:** `journal/bot_state.json` (+ `.bak_*`), `journal/_run_progress.json`.
- **Логи (регенерируемы, в .gitignore):** `cron.log`, `bot.log`, `news_ingest.log`, `reminders.log`.
- Очередей/брокеров (redis/celery) у Оракула **нет** (это инфраструктура чужих проектов).

### 4.2 Конфиги (имена и назначение; значения не печатались)
**Правятся руками:** `config/models.yaml`, `config/universe.yaml`, `config/news.yaml`, `config/rubric.yaml`, `config/costs.yaml`, `config/presentation.yaml`.
**Генерируются кодом (руками НЕ править):** `config/weights.yaml`, `config/thresholds.yaml` (← `ops/calibrate_week4.py`); `knowledge/cascade_sensitivities.yaml` (← `ops/calibrate_sensitivities.py`); `knowledge/cascade_lags_calibrated.yaml` (← `ops/calibrate_cascade_lags.py`).
**Неизменяемый по §11/§12:** `config/limits.yaml` (риск/бюджет/kill-критерий — только подпись пользователя).
**Знание (seed, руками):** `knowledge/causal_links.yaml`, `knowledge/precursors.yaml`, `knowledge/cascade_chains.yaml`, `knowledge/masked_cases/`.

### 4.3 Переменные окружения / секреты
- Файл: **`/root/oracle/.env`** (права `600`, в `.gitignore`). Формат: `export KEY=val`.
- Имена переменных (значения НЕ печатались): `OPENROUTER_API_KEY`, `EODHD_API_KEY`, `NEWSAPI_AI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Проверка наличия: `ops/check_keys.py`. Cron и systemd окружение НЕ наследуют — каждая команда сама делает `. ./.env`.

### 4.4 MCP-серверы
- **Не подключено ни одного MCP-сервера** — ни глобально в `/root/.claude.json`, ни в проекте `/root/oracle`.
- **`maxim-kb` отсутствует** (литерала «maxim» в конфиге Claude нет). Если он ожидался — он НЕ настроен на этой машине.

---

## 5. КАК ЭТО ЗАПУСКАЕТСЯ

- **Демон (бот-пульт):** systemd `oracle-bot.service` → `ops/bot.py` (long-polling, автозапуск, сейчас работает).
- **Рутина:** root-**crontab** (см. §2.5). Каждая строка сама поднимает venv+`.env`. Прямой запуск python (без Claude Code).
- **Разработка/разбор:** интерактивный Claude Code + skills (`/run-funnel`, `/calibrate`, `/review-week`, `/budget`, `/debate`, …).

### Команда одного боевого прогона воронки (как в cron)
```bash
cd /root/oracle && set -a && . ./.env && set +a && \
  /root/oracle/.venv/bin/python orchestrator/run.py --mode event_first --k 2
```
Сухой/безопасный прогон без сети и трат: `… run.py --mode mock`.

---

## ОТКРЫТЫЕ ВОПРОСЫ (нужно уточнить у пользователя)

1. **maxim-kb MCP.** Он упомянут в задаче, но не настроен. Он должен быть подключён (и где живёт KB), или это про другой контур/машину?
2. **Стейл-crontab.** Применять ли репозиторный `ops/crontab.txt` (event-first будни вместо «только понедельник»)? Сейчас установленный отстаёт. *(в режиме чтения не трогал)*
3. **Незакоммиченные 23 файла** (cascade-first Этап ~7, +2055 строк): это незавершённая работа в процессе или забытый коммит? Особенно: `journal/predictions.jsonl` (+39) и `journal/watchlist.jsonl` (+19) — запечатанные журналы лежат незакоммиченными.
4. **Боевой ли event-first?** В cron он есть (понедельник), но в логах преобладает `theme/brent` и калибровка; были ли реальные live-прогоны event-first, или всё ещё mock/dry?
5. **Что считать «настройкой Оракула»** в этой сессии — расписание, вселенная/террейн, веса, модели, бот? Это определит следующий шаг.
6. **Воспроизводимость venv** — нужен ли зафиксированный манифест зависимостей (см. РИСКИ §1)?

---

## РИСКИ / ЗАМЕЧАНИЯ (без рекомендаций по реорганизации)

1. **Нет манифеста зависимостей.** Окружение существует только как `.venv`; `requirements.txt`/`pyproject.toml` отсутствуют → среду нельзя пересоздать детерминированно, версии (numpy 2.4 / pandas 3.0 / scipy 1.17) зафиксированы только де-факто.
2. **Установленный crontab расходится с репозиторием** (event-first: понедельник vs будни). Конфиг расписания живёт в двух местах и рассинхронизирован — легко принять одно за другое.
3. **Дубль определения расписания и юнита.** `ops/crontab.txt` и `ops/oracle-bot.service` — копии того, что реально установлено в системе; нет механизма, гарантирующего их совпадение (правка репо не применяется автоматически).
4. **Запечатанные журналы незакоммичены.** `predictions.jsonl`/`watchlist.jsonl`/`costs.jsonl` имеют незакоммиченные добавления — при П16-инвариантe «ничего не удаляется» это риск потери/расхождения истории. Хук `.claude/hooks/guard_journal.py` защищает от ПЕРЕзаписи, но не от того, что данные не в git.
5. **Поток «нет идей».** Свежий live-функнел снова выдал «стоящих идей нет» (FDR 0 после скана 26). Это легитимный результат по §6, но согласуется с памятью о «пустых днях» — террейн/вход, а не порог (диагноз уже зафиксирован в памяти проекта).
6. **Ворота Б→Д далеко.** 58 прогнозов запечатано, 0 исходов сверено (Brier ещё не считается) — до 270 разрешённых исходов путь длинный; форвард-таймер только идёт.
7. **Соседство с чужими сервисами.** На машине плотно живут другие проекты (mycode docker-стек, relcode, reelsforge, readai) на портах 80/443/8000/8001/5433/8501/3306/3100 и т.д. Оракул изолирован (свой venv, своя БД, исходящий бот), но общая нагрузка/диск/сеть — общие.
8. **GDELT 429 в анамнезе** (память). Сейчас сбор отрабатывает (8985 новостей, 0 сбоев в последнем логе), но backoff-зависимость от внешнего бесплатного API остаётся хрупкой точкой.
9. **Недокументировано централизованно:** часть знаний о запуске/различиях mock/live/боевого живёт в памяти проекта и комментариях, а не в одном README — новому оператору собрать картину тяжело (этот файл — попытка свести её воедино).

---
*Конец карты. Режим только чтение соблюдён: создан единственный файл `DISCOVERY.md`, ничего не запускалось, не менялось и не перезапускалось; секретные значения не печатались.*
