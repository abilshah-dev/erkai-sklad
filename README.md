# ERKai Склад

Telegram-бот и Mini App для оптовой торговли: торговые агенты, склад, бухгалтер, владелец.
Одна кодовая база на всех клиентов. У каждого клиента свой бот, своя база Supabase и свой `.env`.

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

Этап 2 (дальше): продажи клиентам, скидки с лимитами и согласованием, долги, сдача денег, PDF-накладные.
Этап 3: выгрузка документов в 1С, утренняя сводка владельцу.

## Устройство

```
app/main.py     запуск: бот + веб-сервер в одном процессе
app/bot.py      меню, регистрация по приглашению, кнопки подтверждения
app/api.py      API для Mini App (проверка подписи Telegram в app/auth.py)
app/db.py       все запросы к Supabase
app/notify.py   рассылка уведомлений
webapp/         окно Mini App (один HTML-файл)
supabase/       схема базы
tools/          импорт номенклатуры из 1С
deploy/         systemd и nginx
```

Python 3.12. На 3.13 библиотека python-telegram-bot 20.7 не запускается.

## Подключение нового клиента

**1. Бот.** В @BotFather: `/newbot` → получить токен.

**2. База.** Создать проект на supabase.com → SQL Editor → вставить `supabase/schema.sql` → Run.
Скопировать из Project Settings → API: URL и ключ `service_role` (формат `eyJ...`).

**3. Сервер** (первый раз, один на всех клиентов):
```bash
mkdir -p /opt/erkai-sklad/clients && cd /opt/erkai-sklad
git clone https://github.com/abilshah-dev/erkai-sklad.git app
cd app && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp deploy/erkai-sklad@.service /etc/systemd/system/ && systemctl daemon-reload
```

**4. Настройки клиента:**
```bash
mkdir /opt/erkai-sklad/clients/client1
cp /opt/erkai-sklad/app/.env.example /opt/erkai-sklad/clients/client1/.env
nano /opt/erkai-sklad/clients/client1/.env      # токен, Supabase, OWNER_TG_ID, WEBAPP_URL, PORT
```

**5. Домен** (первый раз): DNS-запись `sklad.erkai.kg` → IP сервера. Затем:
```bash
cp deploy/nginx-sklad.erkai.kg.conf /etc/nginx/sites-available/sklad.erkai.kg
ln -s /etc/nginx/sites-available/sklad.erkai.kg /etc/nginx/sites-enabled/
certbot --nginx -d sklad.erkai.kg && systemctl reload nginx
```
Для каждого нового клиента: добавить в этот файл блок `location /clientN/` со своим портом.

**6. Номенклатура из 1С:**
```bash
cd /opt/erkai-sklad/app
ENV_FILE=/opt/erkai-sklad/clients/client1/.env .venv/bin/python -m tools.import_1c остатки.xlsx
```

**7. Запуск:**
```bash
systemctl enable --now erkai-sklad@client1
journalctl -u erkai-sklad@client1 -n 50 --no-pager
```

**8. Сотрудники.** Владелец пишет боту `/start` (его ID в `OWNER_TG_ID`), нажимает «➕ Пригласить сотрудника»
и пересылает ссылку бухгалтеру, складовщику и агентам.

## Обновление

```bash
cd /opt/erkai-sklad/app && git pull && .venv/bin/pip install -r requirements.txt
systemctl restart 'erkai-sklad@*'
```

## Проверка

```bash
.venv/bin/python -m tests.test_api
```
