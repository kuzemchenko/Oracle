#!/bin/bash
# ops/safe_pull.sh — ежечасная БЕЗОПАСНАЯ протяжка облачных коммитов на сервер (архитектура 02.11).
#
# Облачный охотник коммитит reports/ideas/ в master; сервер забирает их каждый час. Ревью 02.11:
# голый `git pull` без фильтра положил бы в канонический журнал любой облачный коммит, задевший
# journal/ (облако журналы не пишет — П16, но гарантию даёт код, а не обещание). Поэтому:
#   1) fetch; 2) если входящие коммиты трогают journal/ (кроме proposals/ и *.md) — ОТКАЗ (код 9 → алерт);
#   3) иначе merge --ff-only (без слияний; расхождение веток → код ≠0 → алерт).
# Новый код бот подхватит сам (тихий перезапуск при смене кода), данные — сразу.
set -u
cd /home/oracle/oracle || exit 1
git fetch -q origin master || exit 2
# Защищено всё в journal/, кроме законных облачных записок (journal/proposals/, *.md в корне) —
# единое правило в ops/journal_guard.sh (перепроверка №2: два расходящихся списка).
. ops/journal_guard.sh || exit 1
incoming=$(incoming_journal_changes HEAD...origin/master)
if [ -n "$incoming" ]; then
  echo "[safe_pull] $(date -u +%FT%TZ) ОТКАЗ: входящие коммиты трогают журналы честности: $incoming"; exit 9
fi
git merge -q --ff-only origin/master || exit 3
echo "[safe_pull] $(date -u +%FT%TZ) ok $(git rev-parse --short HEAD)"
