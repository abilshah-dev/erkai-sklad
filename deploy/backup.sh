#!/usr/bin/env bash
# Резервная копия всех баз клиентов (sklad_*). Хранится 14 дней.
# Запускается по расписанию из /etc/cron.d/erkai-sklad-backup, можно и вручную.
set -euo pipefail
OUT=/opt/erkai-sklad/backups
DAY=$(date +%F)
mkdir -p "$OUT"
for DB in $(sudo -u postgres psql -Atc "select datname from pg_database where datname like 'sklad\_%'"); do
  sudo -u postgres pg_dump -Fc "$DB" > "$OUT/${DB}_${DAY}.dump"
  echo "$(date '+%F %T') $DB → $OUT/${DB}_${DAY}.dump"
done
find "$OUT" -name 'sklad_*.dump' -mtime +14 -delete
