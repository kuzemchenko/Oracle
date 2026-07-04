#!/bin/bash
# ops/cron_alert.sh — обёртка боевых cron-команд «Оракула» (ревью 04.07: тишина при падении).
# Запускает команду; ненулевой код выхода → строка в journal/alerts.jsonl (бот пушит алерт
# следующим tick'ом). Код выхода пробрасывается. Путь журнала переопределяем (тесты):
#   ORACLE_ALERTS=/tmp/x.jsonl ops/cron_alert.sh <метка> <команда...>
ALERTS="${ORACLE_ALERTS:-/home/oracle/oracle/journal/alerts.jsonl}"
LABEL="$1"; shift
"$@"
rc=$?
if [ "$rc" -ne 0 ]; then
  printf '{"ts":"%s","label":"%s","exit":%d}\n' "$(date -u +%FT%TZ)" "$LABEL" "$rc" >> "$ALERTS"
fi
exit $rc
