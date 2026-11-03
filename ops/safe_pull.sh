#!/bin/bash
# ops/safe_pull.sh — ежечасная БЕЗОПАСНАЯ протяжка облачных коммитов на сервер (архитектура 02.11).
#
# Облачный охотник коммитит reports/ideas/ в master; сервер забирает их каждый час. Ревью 02.11:
# голый `git pull` без фильтра положил бы в канонический журнал любой облачный коммит, задевший
# journal/ (облако журналы не пишет — П16, но гарантию даёт код, а не обещание). Поэтому:
#   1) fetch; 2) если входящие коммиты трогают journal/ (кроме proposals/ и *.md) — ОТКАЗ (код 9 → алерт);
#   3) если меняют настройки владельца или охранные скрипты — ОТКАЗ (код 10 → алерт со списком файлов);
#   4) иначе merge --ff-only (без слияний; расхождение веток → код 3 → алерт).
# Новый код бот подхватит сам (тихий перезапуск при смене кода), данные — сразу.
#
# Аудит 03.11 (P0, охранный пакет):
#   • при ЛЮБОМ отказе (9/10/3) идеи облака всё равно доходят: снимок reports/ideas/ из origin/master
#     кладётся в .cache/ideas_incoming/ (вне git — рабочая копия не пачкается, протяжка потом не
#     спотыкается о неотслеживаемые файлы); бот читает идеи оттуда, пока снимок есть. Успешная
#     протяжка снимок удаляет. Так «заморозка» кода не останавливает доставку идеи дня;
#   • flock: протяжка (:05), чекпоинт (22:00, вызывает протяжку сам) и ручные операции не
#     пересекаются; ждём до 15 минут, потом код 8;
#   • ORACLE_DIR — каталог репозитория (по умолчанию боевой; песочница тестов подставляет свой).
set -u
cd "${ORACLE_DIR:-/home/oracle/oracle}" || exit 1
if [ -z "${ORACLE_GIT_LOCKED:-}" ]; then
  exec 9>"$(git rev-parse --git-dir)/oracle_git.lock"
  flock -w 900 9 || { echo "[safe_pull] $(date -u +%FT%TZ) git занят другой операцией больше 15 минут"; exit 8; }
  export ORACLE_GIT_LOCKED=1
fi
git fetch -q origin master || exit 2
# Защищено всё в journal/, кроме законных облачных записок (journal/proposals/, *.md в корне) —
# единое правило в ops/journal_guard.sh (перепроверка №2: два расходящихся списка).
. ops/journal_guard.sh || exit 1

incoming_ideas() {
  # снимок идей из origin/master вне git; атомарная замена (бот не увидит полупустую папку)
  rm -rf .cache/ideas_incoming.tmp && mkdir -p .cache/ideas_incoming.tmp || return 1
  git archive origin/master reports/ideas | tar -x -C .cache/ideas_incoming.tmp || return 1
  rm -rf .cache/ideas_incoming && mv .cache/ideas_incoming.tmp .cache/ideas_incoming
}
refuse() {
  if incoming_ideas; then
    echo "[safe_pull] идеи облака сняты в .cache/ideas_incoming — доставка идёт, код не протянут"
  else
    echo "[safe_pull] снимок идей облака не удался"
  fi
  exit "$1"
}

incoming=$(incoming_journal_changes HEAD...origin/master)
if [ -n "$incoming" ]; then
  echo "[safe_pull] $(date -u +%FT%TZ) ОТКАЗ: входящие коммиты трогают журналы честности: $incoming"; refuse 9
fi
owner_cfg=$(incoming_owner_config_changes HEAD...origin/master)
if [ -n "$owner_cfg" ]; then
  echo "$owner_cfg" > journal/pull_refused.txt      # бот назовёт владельцу конкретные файлы
  echo "[safe_pull] $(date -u +%FT%TZ) ОТКАЗ: входящие коммиты меняют настройки владельца (лимиты/веса/рубрика/пороги): $owner_cfg — только ручная протяжка владельцем"; refuse 10
fi
rm -f journal/pull_refused.txt
git merge -q --ff-only origin/master || { echo "[safe_pull] $(date -u +%FT%TZ) ветки разошлись — нужна ручная протяжка"; refuse 3; }
rm -rf .cache/ideas_incoming .cache/ideas_incoming.tmp     # протяжка прошла — снимок не нужен
echo "[safe_pull] $(date -u +%FT%TZ) ok $(git rev-parse --short HEAD)"
