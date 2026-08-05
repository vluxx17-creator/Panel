"""Точка входа: бот (polling или webhook) + REST API для мини-приложения."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app import bot as bot_module
from app import db
from app.api import cors_middleware, setup_routes
from app.config import BOT_TOKEN, PORT, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_SECRET

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("panel")


async def build_app() -> tuple[web.Application, Bot, Dispatcher]:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    conn = await db.connect()
    bot_module.register(dp, conn)

    app = web.Application(middlewares=[cors_middleware])
    app["db"] = conn
    app["bot"] = bot
    app["notify_admins"] = bot_module.notify_admins
    app["approve_payout"] = bot_module.approve_payout
    app["reject_payout"] = bot_module.reject_payout
    app["broadcast"] = bot_module.broadcast
    setup_routes(app)
    return app, bot, dp


async def main() -> None:
    app, bot, dp = await build_app()

    if WEBHOOK_HOST:
        SimpleRequestHandler(dispatcher=dp, bot=bot,
                             secret_token=WEBHOOK_SECRET or None).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        await bot.set_webhook(WEBHOOK_HOST.rstrip("/") + WEBHOOK_PATH,
                              secret_token=WEBHOOK_SECRET or None,
                              drop_pending_updates=True)
        log.info("webhook: %s%s", WEBHOOK_HOST, WEBHOOK_PATH)
    else:
        await bot.delete_webhook(drop_pending_updates=True)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("API запущен на порту %s", PORT)

    try:
        if WEBHOOK_HOST:
            await asyncio.Event().wait()
        else:
            await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("остановлено")
