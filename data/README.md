# data/ — слой данных «Оракула»

Детерминированные коннекторы (без LLM). Источник истины по §30 п.1: **GDELT + NewsAPI.ai + pytrends**
для новостей и **EODHD** для котировок. Всё кладётся в `storage/oracle.db`.

## Котировки
- `eodhd.py` — дневные EOD-ряды ядра из `config/universe.yaml` (таблица `quotes`). Неделя 1.

## Новостной поток (Неделя 2, §4 «Сборщик новостей», П1)

Суточный мультиязычный поток нормализуется и тегируется **без ручной правки** и дедуплицируется.

| Файл | Что делает |
|---|---|
| `news_common.py` | Схема `news`/`trends`, нормализация, тегирование, дедуп, upsert. Сети и LLM нет. |
| `_maps.py` | Карты язык→ISO 639-1 и страна→ISO 3166-1 alpha-2 (GDELT-имена и ISO-639-3 NewsAPI). |
| `gdelt.py` | GDELT DOC 2.0 (без ключа, мультиязычность). Тела статьи нет — только теги. |
| `newsapi_ai.py` | NewsAPI.ai/EventRegistry getArticles (нужен `NEWSAPI_AI_KEY`). Тегированный поток + тело. |
| `trends.py` | Google Trends через pytrends: ряд интереса + related (таблицы `trends`, `trends_related`). |
| `news_ingest.py` | **Суточный оркестратор для cron**: GDELT+NewsAPI → дедуп → тренды (best-effort) → отчёт. |
| `tests/test_news_common.py` | pytest на детерминизм нормализации/тегирования/дедупа (сеть не нужна). |

### Теги (П1: «язык, страна, тип источника, время»)
- **lang** → ISO 639-1 (`en`,`ru`,`ar`…). Неизвестно → `NULL` («нет данных», П8).
- **country** → ISO 3166-1 alpha-2 (`US`,`RU`,`SA`…). GDELT даёт всегда; NewsAPI — когда знает источник, иначе `NULL`.
- **source_type** → `media` | `social` | `forum` | `official` (СМИ/соцсеть/форум/официоз).
  Соцсеть/форум — строго по домену реальной площадки (по границе ярлыка, не подстрокой);
  `dataType=pr` → official; `dataType=blog` у EventRegistry = немейнстримный сайт → **media**, не соцсеть.
- **published_at** → ISO 8601 UTC.

### Дедупликация
1. **Точная** — на вставке: `id = sha1(каноничный URL)` — PRIMARY KEY. Один URL (без www/трекинга/фрагмента) = одна строка.
2. **Near-dup** — `dedupe_day()`: один сюжет под разными URL/из разных источников группируется по
   отпечатку заголовка + Жаккару токенов (порог 0.82) в пределах суток. Канонический представитель —
   самая ранняя по времени запись; остальным `dup_of=<id>`. Идемпотентно.
- Уникальный поток: `SELECT * FROM news WHERE dup_of IS NULL`. Ничего не удаляется (CLAUDE.md).

### Запуск
```bash
set -a && . ./.env && set +a
.venv/bin/python data/news_ingest.py            # полный суточный проход (cron: ops/crontab.txt)
.venv/bin/python data/news_ingest.py --report   # только отчёт качества по базе
.venv/bin/python data/gdelt.py --status          # что в базе
.venv/bin/python -m pytest data/tests/ -q
```
Охват (темы, языки, ключи трендов, паузы) — в `config/news.yaml`. Меняем охват там, **данные не правим**.

### Известные внешние лимиты (не дефекты кода)
- **GDELT free**: ~1 запрос/5 с; всплеск → HTTP 429 и временный блок IP. Поэтому `pause_sec: 7` и backoff.
- **pytrends/Google Trends**: жёсткий 429 на датацентровых IP. Коннектор исправен (backoff, graceful),
  тренды в суточном проходе — **best-effort и не блокируют новостной поток**. Данные подтянутся
  с разрешённого IP/позже. Доп. нюанс: pytrends 4.9.2 несовместим с urllib3 2.x (`method_whitelist`) —
  поэтому backoff делаем сами, `retries` в `TrendReq` не передаём.
