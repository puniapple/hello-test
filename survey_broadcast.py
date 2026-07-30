# survey_broadcast.py  — добавь в .gitignore, как broadcast_launch.py
import asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select, update
from src.models import User
from src.db import async_session
from src.config import BOT_TOKEN  # поправь импорт

SURVEY_KEY = "hard_job_search_072026"
QUESTION = "Что для тебя сейчас самое сложное в поиске работы?"

async def main():
    bot = Bot(BOT_TOKEN)
    sent = failed = blocked = 0

    async with async_session() as session:
        users = (await session.execute(
            select(User).where(User.is_active == True)
        )).scalars().all()

    print(f"Всего активных: {len(users)}")

    for u in users:
        try:
            await bot.send_message(u.telegram_id, QUESTION)
            async with async_session() as s:
                await s.execute(
                    update(User).where(User.id == u.id).values(survey_awaiting=SURVEY_KEY)
                )
                await s.commit()
            sent += 1
            await asyncio.sleep(0.1)  # ~10 msg/sec, под лимиты Telegram
        except TelegramForbiddenError:
            blocked += 1
            async with async_session() as s:
                await s.execute(
                    update(User).where(User.id == u.id).values(is_active=False)
                )
                await s.commit()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(u.telegram_id, QUESTION)
                async with async_session() as s:
                    await s.execute(
                        update(User).where(User.id == u.id).values(survey_awaiting=SURVEY_KEY)
                    )
                    await s.commit()
                sent += 1
            except Exception:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"fail {u.telegram_id}: {e}")

    await bot.session.close()
    print(f"Отправлено: {sent}, заблокировали: {blocked}, ошибок: {failed}")

if __name__ == "__main__":
    asyncio.run(main())