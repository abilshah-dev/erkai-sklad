"""Настройки берутся из файла .env — у каждого клиента свой .env."""
import os

from dotenv import load_dotenv

# Под systemd переменные приходят из EnvironmentFile. Для ручного запуска можно указать файл:
#   ENV_FILE=/opt/erkai-sklad/clients/client1/.env python -m tools.import_1c файл.xlsx
load_dotenv(os.environ.get("ENV_FILE", ".env"))

BOT_TOKEN = os.environ["BOT_TOKEN"]                    # токен бота клиента от @BotFather
# База клиента на нашем сервере: postgresql://пользователь:пароль@127.0.0.1:5432/база
DATABASE_URL = os.environ["DATABASE_URL"]
# Адрес Mini App, обязательно https и со слешем на конце: https://sklad.erkai.kg/client1/
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/") + "/"
OWNER_TG_ID = int(os.environ.get("OWNER_TG_ID", "0"))  # Telegram ID владельца — регистрируется сам
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Склад")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8081"))
