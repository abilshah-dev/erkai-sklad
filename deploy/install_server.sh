#!/usr/bin/env bash
# Первичная установка ERKai Склад на сервер (Ubuntu 24.04). Запускать один раз от root:
#   bash install_server.sh
# Ничего не трогает в уже работающих ботах (erk-bot, erkai-agent).
set -euo pipefail

echo "== 1/5 Пакеты: PostgreSQL, Python 3.12, nginx, certbot"
apt-get update -q
apt-get install -y -q postgresql python3.12-venv nginx certbot python3-certbot-nginx git

echo "== 2/5 PostgreSQL слушает только этот сервер (не интернет)"
systemctl enable --now postgresql
PGCONF=$(ls /etc/postgresql/*/main/postgresql.conf | head -1)
grep -q "^listen_addresses = 'localhost'" "$PGCONF" || echo "listen_addresses = 'localhost'" >> "$PGCONF"
systemctl restart postgresql

echo "== 3/5 Код"
mkdir -p /opt/erkai-sklad/clients /opt/erkai-sklad/backups
if [ ! -d /opt/erkai-sklad/app/.git ]; then
  git clone https://github.com/abilshah-dev/erkai-sklad.git /opt/erkai-sklad/app
fi
cd /opt/erkai-sklad/app
python3.12 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== 4/5 Служба systemd (шаблон для всех клиентов)"
cp deploy/erkai-sklad@.service /etc/systemd/system/
systemctl daemon-reload

echo "== 5/5 Ежедневная резервная копия всех баз в 03:00"
chmod +x deploy/backup.sh deploy/new_client.sh
echo "0 3 * * * root /opt/erkai-sklad/app/deploy/backup.sh >> /var/log/erkai-sklad-backup.log 2>&1" > /etc/cron.d/erkai-sklad-backup

echo
echo "Готово. Следующий шаг — первый клиент:"
echo "  bash /opt/erkai-sklad/app/deploy/new_client.sh client1 8081"
