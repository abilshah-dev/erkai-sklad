"""Точка входа: в одном процессе работают Telegram-бот и веб-сервер для Mini App.

Запуск:  python -m app.main
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web
from telegram.ext import Application

from . import api, bot, config, db, notify

BISHKEK = timezone(timedelta(hours=6))
SUMMARY_HOUR = 20  # итоги дня владельцу в 20:00 по Бишкеку


async def daily_summary(tg_bot):
    """Каждый день в 20:00 отправляет владельцу итоги дня."""
    while True:
        now = datetime.now(BISHKEK)
        nxt = now.replace(hour=SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            text = (await notify.summary_text()).replace("📊 Сводка", "📊 Итоги дня", 1)
            for u in await db.users_by_roles(["owner"]):
                await tg_bot.send_message(u["tg_id"], text)
        except Exception:
            logging.exception("Итоги дня не отправлены")


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
        summary_task = asyncio.create_task(daily_summary(application.bot))
        logging.info("Бот запущен, Mini App на http://%s:%s", config.HOST, config.PORT)
        try:
            await asyncio.Event().wait()             # работаем, пока процесс не остановят
        finally:
            summary_task.cancel()
            await application.updater.stop()
            await application.stop()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
