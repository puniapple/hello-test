"""Запуск модуля канала вручную.

    python run_channel.py          — сухой прогон: печатает карточки, ничего не шлёт
    python run_channel.py --post   — публикует в канал из src/channel/config.py
    --no-linkedin                  — как дневной цикл: без запросов к LinkedIn
"""
import asyncio
import sys

from src.channel.poster import run_channel_cycle
from src.config import settings


async def main():
    dry = "--post" not in sys.argv
    bot = None
    if not dry:
        from aiogram import Bot
        bot = Bot(settings.telegram_bot_token)
    try:
        await run_channel_cycle(bot, dry_run=dry, fetch_linkedin="--no-linkedin" not in sys.argv)
    finally:
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
