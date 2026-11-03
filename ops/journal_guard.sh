#!/bin/bash
# ops/journal_guard.sh — ЕДИНЫЙ список того, что облако не вправе менять в journal/ (П16).
#
# Перепроверка №2 (03.11): safe_pull.sh и checkpoint.sh держали СВОИ списки защищённых журналов, и они
# расходились (holdout_access.log, replay_logs/ были только в одном; журналы решений владельца,
# протоколы /debate, очередь прокачки — ни в одном). Теперь правило одно и обратное: облако журналы
# НЕ пишет, поэтому защищено ВСЁ в journal/, кроме того, что облачные сессии коммитят законно —
# подписи и предложения владельцу (journal/proposals/) и текстовые записки *.md в корне journal/.
#
# Использование (source): incoming_journal_changes <диапазон git diff> → печатает до 5 путей.
JOURNAL_CLOUD_ALLOWED='^journal/(proposals/|[^/]+\.md$)'

incoming_journal_changes() {
  git diff --name-only "$1" -- journal/ | grep -Ev "$JOURNAL_CLOUD_ALLOWED" | head -5
}

# Перепроверка №3 (03.11): ежечасная протяжка + тихий перезапуск бота применяли бы облачные правки
# лимитов и kill-критериев (Инв#4/#5), весов скоринга и рубрики суда (меняются только с согласия
# владельца) без его «ок». Такие входящие коммиты safe_pull НЕ применяет (код 10 → алерт владельцу);
# применить их можно только ручной протяжкой владельца (команда в CLAUDE.md).
OWNER_CONFIGS='^config/(limits|weights|rubric|thresholds)\.yaml$'

incoming_owner_config_changes() {
  git diff --name-only "$1" -- config/ | grep -E "$OWNER_CONFIGS" | head -5
}
