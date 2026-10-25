#!/usr/bin/env bash
# Подключение нового клиента: своя база, свой .env, своя служба.
#   bash new_client.sh <имя_латиницей> <порт>
#   bash new_client.sh client1 8081
set -euo pipefail

NAME=${1:?Укажите имя клиента латиницей, например client1}
PORT=${2:?Укажите порт, например 8081}
[[ "$NAME" =~ ^[a-z][a-z0-9_]{1,30}$ ]] || { echo "Имя: маленькие латинские буквы, цифры, _"; exit 1; }

APP=/opt/erkai-sklad/app
DIR=/opt/erkai-sklad/clients/$NAME
DB=sklad_$NAME
[ -e "$DIR/.env" ] && { echo "Клиент $NAME уже есть: $DIR/.env"; exit 1; }

echo "== База $DB"
PASS=$(openssl rand -hex 16)
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
create role $DB login password '$PASS';
create database $DB owner $DB;
SQL
PGPASSWORD=$PASS psql -h 127.0.0.1 -U "$DB" -d "$DB" -v ON_ERROR_STOP=1 -q -f "$APP/db/schema.sql"

echo "== Настройки $DIR/.env"
mkdir -p "$DIR"
cat > "$DIR/.env" <<ENV
COMPANY_NAME=$NAME
BOT_TOKEN=ВСТАВЬТЕ_ТОКЕН_ОТ_BOTFATHER
OWNER_TG_ID=0
DATABASE_URL=postgresql://$DB:$PASS@127.0.0.1:5432/$DB
WEBAPP_URL=https://sklad.erkai.kg/$NAME/
HOST=127.0.0.1
PORT=$PORT
ENV
chmod 600 "$DIR/.env"

echo
echo "Готово. Дальше:"
echo "  1) nano $DIR/.env   — вписать COMPANY_NAME, BOT_TOKEN, OWNER_TG_ID"
echo "  2) добавить в nginx блок location /$NAME/ → 127.0.0.1:$PORT (см. deploy/nginx-sklad.erkai.kg.conf)"
echo "  3) systemctl enable --now erkai-sklad@$NAME"
