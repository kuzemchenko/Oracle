#!/bin/bash
# ops/safe_pull.sh — ежечасная БЕЗОПАСНАЯ протяжка облачных коммитов на сервер (архитектура 02.11).
#
# Облачный охотник коммитит reports/ideas/ в master; сервер забирает их каждый час. Ревью 02.11:
# голый `git pull` без фильтра положил бы в канонический журнал любой облачный коммит, задевший
# journal/ (облако журналы не пишет — П16, но гарантию даёт код, а не обещание). Поэтому:
#   1) fetch; 2) если входящие коммиты трогают journal/ — ОТКАЗ (код 9 → алерт в бота);
#   3) иначе merge --ff-only (без слияний; расхождение веток → код ≠0 → алерт).
# Новый код бот подхватит сам (тихий перезапуск при смене кода), данные — сразу.
set -u
cd /home/oracle/oracle || exit 1
git fetch -q origin master || exit 2
incoming=$(git diff --name-only HEAD...origin/master -- journal/ | head -5)
if [ -n "$incoming" ]; then
  echo "[safe_pull] $(date -u +%FT%TZ) ОТКАЗ: входящие коммиты трогают journal/: $incoming"; exit 9
fi
git merge -q --ff-only origin/master || exit 3
echo "[safe_pull] $(date -u +%FT%TZ) ok $(git rev-parse --short HEAD)"
