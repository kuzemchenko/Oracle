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
