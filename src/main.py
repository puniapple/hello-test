"""Application entry point with bot polling + scheduled job search."""

import asyncio
import logging

import structlog
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.bot.handlers.commands import router as commands_router
from src.bot.handlers.cv_upload import router as cv_upload_router
from src.bot.handlers.profile_edit import router as profile_edit_router
from src.bot.handlers.reactions import router as reactions_router
from src.bot.handlers.voice import router as voice_router
from src.config import settings
from src.workers.job_search import run_job_search_cycle


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

    missing = []
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not settings.database_url or "localhost" in settings.database_url:
        missing.append("DATABASE_URL (must point to Neon)")
    if missing:
        log.error("env_vars_missing", missing=missing)
        return

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(commands_router)
    dp.include_router(reactions_router)
    dp.include_router(cv_upload_router)
    dp.include_router(voice_router)
    dp.include_router(profile_edit_router)

    # Scheduled job search every 8 hours
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_job_search_cycle,
        trigger=CronTrigger(hour="5, 13, 21"),
        args=[bot],
        id="job_search_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.start()
    log.info("scheduler_started", interval="3x daily at 5/13/21 UTC")

    log.info("polling_started")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())