#!/bin/bash
# ops/checkpoint.sh — вечерний чекпоинт журналов и отчётов в GitHub (добавлен 31.10.2026).
#
# ПОЧЕМУ: с 15.07 по 31.10.2026 на GitHub не ушло ни одного коммита — «вечерний чекпоинт»
# каждый день делала РУКОЙ Claude-сессия, задачи в cron никогда не было. Конвейер всё это время
# работал (≈870–900 прогнозов и сверок в месяц), но 3,5 месяца журналов лежали только на сервере.
#
# ЧТО ДЕЛАЕТ: git add ТОЛЬКО journal/ reports/ ops/reports/ (код НЕ трогает — он уезжает коммитами
# разработки), коммит при наличии изменений, push master целиком (в т.ч. застрявшие локально
# коммиты разработки). Журналы НЕ редактируются (П16) — только фиксируются как есть.
#
# ЗАПУСК: от пользователя oracle (git-записи только от него), через ops/cron_alert.sh —
# ненулевой выход пишется в journal/alerts.jsonl и уходит владельцу в бота.
set -u
cd /home/oracle/oracle || exit 1

if [ -f .git/MERGE_HEAD ] || [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
  echo "[checkpoint] репозиторий в середине merge/rebase — пропуск"; exit 3
fi
# Чужие staged-изменения (интерактивная сессия разработки) — не подхватываем в журнальный коммит.
if ! git diff --cached --quiet; then
  echo "[checkpoint] в индексе чужие изменения — пропуск"; exit 4
fi

# Архитектура 02.11: облако (охотник) коммитит в master днём — сначала подтягиваем, чтобы
# журнальный коммит лёг поверх свежего origin (иначе ветки расходятся и встаёт ежечасный pull).
ops/safe_pull.sh || echo "[checkpoint] безопасная протяжка не прошла — продолжаю"

git add -A -- journal reports ops/reports || exit 5
if git diff --cached --quiet; then
  echo "[checkpoint] $(date -u +%F): нечего коммитить"
else
  n=$(git diff --cached --name-only | wc -l)
  git commit -q -m "journal: чекпоинт $(date -u +%F) — ${n} файл(ов) (авто, ops/checkpoint.sh)" || exit 6
fi

# Пуш всего master. Расхождение с origin (кто-то запушил мимо сервера) НЕ сливаем автоматически —
# это ручной разбор; падение пуша = алерт.
if ! git push -q origin master; then
  # гонка с облачным коммитом между pull и push: пересаживаем ЛОКАЛЬНЫЕ коммиты (журналы/
  # отчёты сервера; облако journal/ не пишет — П16) поверх origin и пробуем ещё раз
  git fetch -q origin master
  . ops/journal_guard.sh || exit 1                 # то же правило, что у safe_pull (перепроверка №2)
  if [ -n "$(incoming_owner_config_changes HEAD...origin/master)" ]; then
    echo "[checkpoint] входящие облачные коммиты меняют лимиты/веса/рубрику/пороги — пересадку не делаю (нужно «ок» владельца)"; exit 7
  fi
  if [ -n "$(incoming_journal_changes HEAD...origin/master)" ]; then
    echo "[checkpoint] входящие облачные коммиты трогают journal/ — пересадку не делаю (П16)"; exit 7
  fi
  # перепроверка №4 (TOCTOU): пересаживаем на ТОТ снимок origin/master, который только что проверили,
  # без второго fetch внутри pull
  if git rebase -q --autostash origin/master && git push -q origin master; then
    echo "[checkpoint] push прошёл после пересадки поверх облачных коммитов"
  else
    git rebase --abort 2>/dev/null
    echo "[checkpoint] push НЕ прошёл (расхождение с origin? SSH-ключ?) — нужен ручной разбор"; exit 7
  fi
fi
echo "[checkpoint] $(date -u +%FT%TZ) ok $(git rev-parse --short HEAD)"
