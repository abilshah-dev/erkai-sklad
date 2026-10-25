"""Точка входа: в одном процессе работают Telegram-бот и веб-сервер для Mini App.

Запуск:  python -m app.main
"""
import asyncio
import logging

from aiohttp import web
from telegram.ext import Application

from . import api, bot, config


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    application = Application.builder().token(config.BOT_TOKEN).build()
    bot.setup(application)

    runner = web.AppRunner(api.make_app(application.bot))
    await runner.setup()
    site = web.TCPSite(runner, config.HOST, config.PORT)

    async with application:
        await application.bot.delete_webhook()       # на случай, если раньше был webhook
        await application.start()
        await application.updater.start_polling()
        await site.start()
        logging.info("Бот запущен, Mini App на http://%s:%s", config.HOST, config.PORT)
        try:
            await asyncio.Event().wait()             # работаем, пока процесс не остановят
        finally:
            await application.updater.stop()
            await application.stop()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
