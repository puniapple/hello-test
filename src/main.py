"""Application entry point."""

import asyncio
import logging

import structlog
from aiogram import Bot, Dispatcher

from src.bot.handlers.commands import router as commands_router
from src.bot.handlers.profile_edit import router as profile_edit_router
from src.config import settings
from src.bot.handlers.cv_upload import router as cv_upload_router
from src.bot.handlers.voice import router as voice_router


def configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ]
    )


async def main() -> None:
    configure_logging()
    log = structlog.get_logger()
    log.info("starting_bot", environment=settings.environment)

    if not settings.telegram_bot_token:
        log.error("telegram_bot_token_missing")
        return

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(commands_router)
    dp.include_router(cv_upload_router)
    dp.include_router(voice_router)
    dp.include_router(profile_edit_router)
    log.info("polling_started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())