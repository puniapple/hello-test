"""Application entry point with bot polling + scheduled job search + Tribute webhooks."""

import asyncio
import logging
import os

import structlog
from aiogram import Bot, Dispatcher
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.bot.handlers.admin import router as admin_router
from src.bot.handlers.commands import router as commands_router
from src.bot.handlers.cv_upload import router as cv_upload_router
from src.bot.handlers.payments import router as payments_router
from src.bot.handlers.profile_edit import router as profile_edit_router
from src.bot.handlers.reactions import router as reactions_router
from src.bot.handlers.cover_letter import router as cover_letter_router
from src.bot.handlers.resume import router as resume_router
from src.bot.handlers.score_breakdown import router as score_breakdown_router
from src.bot.handlers.voice import router as voice_router
from src.config import settings
from src.db.session import async_session
from src.services.billing import downgrade_expired_subscriptions, send_renewal_reminders
from src.web.server import create_web_app
from src.workers.job_search import run_job_search_cycle
from src.bot.handlers.survey import router as survey_router

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

    if not settings.tribute_api_key:
        log.warning(
            "tribute_api_key_missing",
            note="платёжная интеграция не активна; /upgrade будет отвечать ошибкой",
        )

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    # Middleware — one-shot paywall уведомление неактивным Free
    from src.bot.middlewares.paywall_notice import PaywallNoticeMiddleware
    dp.message.middleware(PaywallNoticeMiddleware())
    dp.callback_query.middleware(PaywallNoticeMiddleware())
    dp.include_router(commands_router)
    dp.include_router(survey_router)
    dp.include_router(reactions_router)
    dp.include_router(cover_letter_router)
    dp.include_router(resume_router)
    dp.include_router(score_breakdown_router)
    dp.include_router(cv_upload_router)
    dp.include_router(voice_router)
    dp.include_router(profile_edit_router)
    dp.include_router(admin_router)
    dp.include_router(payments_router)
    

    # --- aiohttp сервер для Tribute webhooks ---
    web_app = create_web_app(bot)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", settings.webhook_port))
    site = web.TCPSite(runner, settings.webhook_host, port)
    await site.start()
    log.info(
        "webhook_server_started",
        host=settings.webhook_host,
        port=port,
        path=settings.tribute_webhook_path,
    )

    # --- Scheduler: job search + expire subscriptions ---
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_job_search_cycle,
        trigger=CronTrigger(hour="6, 12, 18"),
        args=[bot],
        id="job_search_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    async def expire_subscriptions_job():
        async with async_session() as session:
            result = await downgrade_expired_subscriptions(session, bot=bot)
            if result and any(result.values()):
                log.info("subscriptions_expired", **result)
    
    async def renewal_reminders_job():
        async with async_session() as session:
            result = await send_renewal_reminders(session, bot=bot)
            if result and any(result.values()):
                log.info("renewal_reminders", **result)
    
    scheduler.add_job(
        expire_subscriptions_job,
        trigger=IntervalTrigger(hours=1),
        id="expire_subscriptions",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        renewal_reminders_job,
        trigger=CronTrigger(hour=9),  # раз в день в 9 UTC = 12 MSK
        id="renewal_reminders",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    
    log.info(
            "scheduler_started",
            jobs=["job_search_cycle (6/12/18 UTC)", "expire_subscriptions (hourly)", "renewal_reminders (daily 9 UTC)"],
        )

    log.info("polling_started")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
