# ERKai Склад

Telegram-бот и Mini App для оптовой торговли: торговые агенты, склад, бухгалтер, владелец.
Одна кодовая база на всех клиентов. У каждого клиента свой бот, своя база PostgreSQL и свой `.env`.
Всё работает на нашем сервере, без платных облачных сервисов.

## Что умеет (этап 1: ядро и склад)

| Роль | Возможности |
|---|---|
| Агент | Корзина-заявка на склад (поиск, категории, остатки), «Моя машина», возврат остатков, свои клиенты |
| Складовщик | Уведомление о заявке → «Выдать как в заявке» или исправить фактическое количество |
| Бухгалтер | Копия каждого движения, журнал, товар у агентов, база всех клиентов, передача клиентов, приглашения |
| Владелец | Всё, что у бухгалтера, плюс приглашение бухгалтера |

Правила, встроенные в базу:
- лимит подотчёта агента (`agent_stock_limit` в таблице `settings`);
- выдачу и возврат подтверждает второй человек, агент сам себе подтвердить не может;
- каждое движение записывается в журнал `movements`, оттуда пойдёт выгрузка в 1С;
- клиенты принадлежат компании: при увольнении агента бухгалтер передаёт их другому одной кнопкой.

## Этап 2: продажи и деньги

| Роль | Возможности |
|---|---|
| Агент | Продажа клиенту из машины, скидка по строкам, оплата наличными / переводом / в долг / частично, PDF-накладная в чат, приём оплат по долгам, «💰 Мои деньги», сдача наличных |
| Владелец | Одобрение скидок выше лимита (кнопкой в чате или во вкладке «🏷 Скидки»), сводка, итоги дня в 20:00, настройки лимитов |
| Бухгалтер | Подтверждение сданных наличных («💵 Касса»), копии продаж и оплат, сводка |

Правила: скидка выше лимита группы → к владельцу; лимит долга на клиента; долг старше N дней → продажа только с оплатой сразу;
оплата гасит самые старые долги первыми; деньги числятся за агентом, пока бухгалтер не подтвердил получение.

Этап 3: выгрузка документов в 1С.

## Устройство

```
app/main.py     запуск: бот + веб-сервер в одном процессе
app/bot.py      меню, регистрация по приглашению, кнопки подтверждения
app/api.py      API для Mini App (проверка подписи Telegram в app/auth.py)
app/db.py       все запросы к базе
app/notify.py   рассылка уведомлений
webapp/         окно Mini App (один HTML-файл)
db/schema.sql   схема базы
tools/          импорт номенклатуры из 1С
deploy/         установка сервера, новый клиент, резервные копии, systemd, nginx
tests/          проверки
```

Python 3.12 (на 3.13 библиотека python-telegram-bot 20.7 не запускается).

## Установка сервера (один раз)

```bash
cd /tmp && git clone https://github.com/abilshah-dev/erkai-sklad.git && bash erkai-sklad/deploy/install_server.sh
```
Ставит PostgreSQL (доступен только с самого сервера), Python, nginx, certbot, код в `/opt/erkai-sklad/app`
и ежедневную резервную копию баз в 03:00 (`/opt/erkai-sklad/backups`, хранится 14 дней).

Домен: DNS-запись **A** `sklad` → `134.209.246.89`, затем:
```bash
cp /opt/erkai-sklad/app/deploy/nginx-sklad.erkai.kg.conf /etc/nginx/sites-available/sklad.erkai.kg
ln -s /etc/nginx/sites-available/sklad.erkai.kg /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx && certbot --nginx -d sklad.erkai.kg
```

## Новый клиент

1. Бот в @BotFather: `/newbot` → токен.
2. На сервере:
   ```bash
   bash /opt/erkai-sklad/app/deploy/new_client.sh client1 8081
   nano /opt/erkai-sklad/clients/client1/.env      # COMPANY_NAME, BOT_TOKEN, OWNER_TG_ID
   ```
   Скрипт сам создаёт базу с паролем и прописывает её в `.env`.
3. В `/etc/nginx/sites-available/sklad.erkai.kg` — блок `location /client1/` с портом клиента, `systemctl reload nginx`.
4. Номенклатура из 1С (Excel-выгрузка «Остатки товаров»):
   ```bash
   cd /opt/erkai-sklad/app
   ENV_FILE=/opt/erkai-sklad/clients/client1/.env .venv/bin/python -m tools.import_1c остатки.xlsx
   ```
5. Запуск и логи:
   ```bash
   systemctl enable --now erkai-sklad@client1
   journalctl -u erkai-sklad@client1 -n 50 --no-pager
   ```
6. Владелец пишет боту `/start` и через «➕ Пригласить сотрудника» отправляет ссылки бухгалтеру, складовщику, агентам.

## Обновление

```bash
cd /opt/erkai-sklad/app && git pull && .venv/bin/pip install -q -r requirements.txt
for c in /opt/erkai-sklad/clients/*/; do ENV_FILE=$c.env .venv/bin/python -m tools.migrate; done
systemctl restart 'erkai-sklad@*'
```
Миграции базы лежат в `db/migrations/`, каждая применяется один раз.

## Резервная копия и восстановление

```bash
bash /opt/erkai-sklad/app/deploy/backup.sh                       # копия сейчас
sudo -u postgres pg_restore -c -d sklad_client1 /opt/erkai-sklad/backups/sklad_client1_2026-10-25.dump
```

## Проверки

```bash
.venv/bin/python -m tests.test_api                                  # без базы
TEST_DATABASE_URL=postgresql://... .venv/bin/python -m tests.test_e2e   # на пустой тестовой базе
```
