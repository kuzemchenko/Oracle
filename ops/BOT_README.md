# Бот-пульт «Оракула» (раздел «Интерфейс»)

Канал человеко-решения (П12 «решение о риске — за человеком», §12 «защита пользователя от себя»,
§15 наблюдаемость, §17 лист ожидания). Telegram-бот на long-polling, живёт как systemd-сервис.

## Что делает
- **Пуш отчётов** прогона (13 полей §8) с кнопками **Принять / Отклонить / Отложить**.
- **Пауза пре-коммитмента §12:** кнопка «Принять» разблокируется через **24 ч** от выдачи идеи
  (для «медленных» каскадных идей). «Отклонить»/«Отложить» — сразу.
- **Журнал решений** `journal/decisions_user.jsonl` (append-only, под guard-хуком): принял/отклонил/
  отложил + **мотив** (снимается ответным сообщением после нажатия).
- **Утренняя строка бюджета** §15 (раз в сутки) и **алерты бюджета** (потолки `config/limits.yaml`,
  инвариант 5): ВНИМАНИЕ ≥80% / ПРЕВЫШЕНИЕ ≥100% → прогоны стоп.
- **Алерты листа ожидания** §17: структурный ценовой триггер (актив пересёк уровень по `oracle.db`).
  РАНО-идеи прогона автоматически попадают в лист как «ручная проверка»; уровень привязывает оператор.

Команды в чате: `/help`, `/status` (открытые идеи), `/budget` (строка сейчас), `/watchlist`.

## Настройка (один раз)
1. Создай бота у @BotFather → получишь **токен**.
2. Узнай свой **chat_id**: запусти сервис без `TELEGRAM_CHAT_ID`, напиши боту любое сообщение —
   он ответит твоим chat_id. Либо через @userinfobot.
3. Добавь в `/root/oracle/.env` (формат — строки `export`):
   ```
   export TELEGRAM_BOT_TOKEN=123456:ABC...
   export TELEGRAM_CHAT_ID=123456789
   ```
   `TELEGRAM_CHAT_ID` — **allow-list**: бот реагирует на кнопки только из этого чата.

## Установка сервиса (переживает перезагрузки)
```bash
sudo cp /root/oracle/ops/oracle-bot.service /etc/systemd/system/oracle-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now oracle-bot
systemctl status oracle-bot      # проверка
journalctl -u oracle-bot -f      # или: tail -f /root/oracle/journal/bot.log
```
После правки `.env`: `sudo systemctl restart oracle-bot`.

Проверка конфигурации без запуска петли: `.venv/bin/python ops/bot.py --check`.

## Лист ожидания — CLI
```bash
# структурный триггер (бот сам сверит с oracle.db и пришлёт алерт):
.venv/bin/python ops/bot_watchlist.py add --asset Brent --symbol BNO.US --level 80 --dir above --direction long
# свободный текст (без авто-алерта — П8, только напоминание):
.venv/bin/python ops/bot_watchlist.py add --asset Copper --text "войти когда дефицит на LME"
.venv/bin/python ops/bot_watchlist.py list
.venv/bin/python ops/bot_watchlist.py cancel <id>
```

## Замечания по инвариантам
- `journal/decisions_user.jsonl` — **запечатанный** журнал (guard_journal.py): только append из
  бот-процесса, ничего не удаляется/не правится (§16). Мотив — отдельная append-строка-аннотация.
- Бот **не имеет мнения о рынке** — он только доставляет отчёты и фиксирует решения человека.
- Вероятности и идеи приходят из воронки; бот ничего не считает про edge.
- `journal/bot_state.json` — внутреннее мутабельное состояние бота (дедуп пушей/алертов), НЕ журнал.
