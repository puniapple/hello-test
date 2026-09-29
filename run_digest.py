"""Дайджест вручную.

    python run_digest.py          — показать, ничего не публикуя
    python run_digest.py --post   — опубликовать в канал
"""
import asyncio
import sys

from src.channel.digest import run_digest
from src.config import settings


async def main():
    dry = "--post" not in sys.argv
    bot = None
    if not dry:
        from aiogram import Bot
        bot = Bot(settings.telegram_bot_token)
    try:
        await run_digest(bot, dry_run=dry)
    finally:
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
