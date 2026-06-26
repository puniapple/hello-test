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
from src.bot.handlers.voice import router as voice_router
from src.config import settings
from src.db.session import async_session
from src.services.billing import (
    downgrade_expired_subscriptions,
    find_onetime_users_to_remind,
    mark_reminder_sent,
)
from src.web.server import create_web_app
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


# --- Cron jobs для подписок ---


async def expire_subscriptions_job() -> None:
    """Раз в час: помечает expired у юзеров с истёкшим plan_expires_at."""
    log = structlog.get_logger(__name__)
    async with async_session() as session:
        count = await downgrade_expired_subscriptions(session)
        if count:
            log.info("expire_subscriptions_done", count=count)


async def remind_onetime_expiring_job(bot: Bot) -> None:
    """Раз в день в 9:00 UTC: напомнить разовым юзерам про скорое окончание Pro."""
    log = structlog.get_logger(__name__)
    async with async_session() as session:
        users = await find_onetime_users_to_remind(session)

    for user in users:
        expires_str = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "скоро"
        try:
            await bot.send_message(
                user.telegram_id,
                f"⏰ Твой Pro заканчивается {expires_str}.\n\n"
                "Можешь продлить через /upgrade — разово или сразу подпиской с автопродлением."
            )
            async with async_session() as session:
                await mark_reminder_sent(session, user.id)
        except Exception:
            log.exception("remind_failed", user_id=user.id, telegram_id=user.telegram_id)


async def main() -> None:
    configure_logging()
    log = structlog.get_logger()
    log.info("starting_bot", environment=settings.environment)

    # --- Проверка обязательных env переменных ---
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

    # --- Мягкое предупреждение по Tribute ---
    if not settings.tribute_api_key:
        log.warning(
            "tribute_api_key_missing",
            note="платёжная интеграция отключена; команды /upgrade и т.п. не будут работать"
        )

    # --- Bot + Dispatcher ---
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(commands_router)
    dp.include_router(reactions_router)
    dp.include_router(cv_upload_router)
    dp.include_router(voice_router)
    dp.include_router(profile_edit_router)
    dp.include_router(admin_router)
    dp.include_router(payments_router)

    # --- aiohttp сервер для Tribute webhook'ов ---
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

    # --- Scheduler ---
    scheduler = AsyncIOScheduler()

    # Цикл поиска вакансий (твой существующий)
    scheduler.add_job(
        run_job_search_cycle,
        trigger=CronTrigger(hour="9, 15, 21"),
        args=[bot],
        id="job_search_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # Раз в час: downgrade истёкших Pro
    scheduler.add_job(
        expire_subscriptions_job,
        trigger=IntervalTrigger(hours=1),
        id="expire_subscriptions",
        max_instances=1,
        coalesce=True,
    )

    # Раз в день в 9:00 UTC: напоминание разовым про скорое окончание
    scheduler.add_job(
        remind_onetime_expiring_job,
        trigger=CronTrigger(hour=9, minute=0),
        args=[bot],
        id="remind_onetime_expiring",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    log.info(
        "scheduler_started",
        jobs=["job_search_cycle (9/15/21 UTC)", "expire_subscriptions (every 1h)", "remind_onetime_expiring (9:00 UTC)"]
    )

    # --- Polling ---
    log.info("polling_started")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
