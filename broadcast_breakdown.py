"""Broadcast запуска фичи разбора соответствия — только сегодняшним получателям.

Отправляет сообщение только тем, кто получил хотя бы одну вакансию
за сегодня (с 00:00 UTC). Free и Pro/Grandfather — разные тексты.

Запуск: python3 broadcast_breakdown_today.py
Лежит в .gitignore, коммитить не надо.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import distinct, select

from src.db.models import User, VacancyMatch
from src.db.session import async_session, engine

from src.config import settings
BOT_TOKEN = settings.telegram_bot_token
if not BOT_TOKEN:
    raise RuntimeError("telegram_bot_token не задан в settings")

DELAY_BETWEEN_SENDS_SEC = 0.5

TEXT_FREE = (
    "Привет! Теперь я умею оценивать вакансии — показываю, "
    "насколько вакансия тебе подходит, где ты совпадаешь с профилем компании, "
    "а где различия. Больше не нужно гадать, стоит откликаться или нет.\n\n"
    "Попробуй по кнопке «🎯 Оценить совпадение»"
)

TEXT_PAID = (
    "Привет! Теперь я умею оценивать вакансии — показываю, "
    "насколько вакансия тебе подходит, где ты совпадаешь с профилем компании, "
    "а где различия. Больше не нужно гадать, стоит откликаться или нет.\n\n"
    "Попробуй по кнопке «🎯 Показать совпадения»"
)


async def collect_recipients() -> tuple[list[User], list[User]]:
    """Собираем юзеров, кому сегодня (с 00:00 UTC) доставлена хоть одна вакансия."""
    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    async with async_session() as s:
        # user_id тех, кому доставлено сегодня
        subq = (
            select(distinct(VacancyMatch.user_id))
            .where(VacancyMatch.sent_at >= today_utc)
        ).subquery()

        result = await s.execute(
            select(User)
            .where(User.id.in_(select(subq)))
            .where(User.is_active.is_(True))
        )
        users = result.scalars().all()

    free_users: list[User] = []
    paid_users: list[User] = []
    for u in users:
        plan = (u.plan or "free").lower()
        if plan in ("pro", "grandfather"):
            paid_users.append(u)
        else:
            free_users.append(u)

    return free_users, paid_users


async def send_broadcast(bot: Bot, users: list[User], text: str, segment_name: str) -> dict:
    stats = {"segment": segment_name, "total": len(users), "sent": 0, "blocked": 0, "errors": 0}
    if not users:
        print(f"\n=== Сегмент {segment_name}: 0 юзеров — пропускаем")
        return stats

    print(f"\n=== Сегмент {segment_name}: {len(users)} юзеров ===")

    for i, user in enumerate(users, 1):
        username = f"@{user.telegram_username}" if user.telegram_username else "—"
        try:
            await bot.send_message(chat_id=user.telegram_id, text=text)
            stats["sent"] += 1
            print(f"  [{i}/{len(users)}] ✅ {username} (id={user.telegram_id})")
        except TelegramAPIError as e:
            err_str = str(e).lower()
            if "blocked" in err_str or "bot was blocked" in err_str or "user is deactivated" in err_str:
                stats["blocked"] += 1
                print(f"  [{i}/{len(users)}] 🚫 {username}: заблокировали бота")
            else:
                stats["errors"] += 1
                print(f"  [{i}/{len(users)}] ❌ {username}: {e}")
        except Exception as e:
            stats["errors"] += 1
            print(f"  [{i}/{len(users)}] ❌ {username}: {e}")

        await asyncio.sleep(DELAY_BETWEEN_SENDS_SEC)

    return stats


async def main():
    free_users, paid_users = await collect_recipients()

    now_utc = datetime.now(timezone.utc)
    print("=" * 60)
    print(f"BROADCAST: фича разбора соответствия")
    print(f"Сейчас: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Условие: получили хотя бы одну вакансию с сегодняшнего 00:00 UTC")
    print("=" * 60)
    print(f"Free с профилем + доставкой сегодня:            {len(free_users)}")
    print(f"Pro/Grandfather с профилем + доставкой сегодня: {len(paid_users)}")
    print(f"Итого:                                          {len(free_users) + len(paid_users)}")
    print()

    if not free_users and not paid_users:
        print("Никого нет в выборке. Завершаю.")
        await engine.dispose()
        return

    print("Текст для Free:")
    print("-" * 60)
    print(TEXT_FREE)
    print("-" * 60)
    print()
    print("Текст для Pro/Grandfather:")
    print("-" * 60)
    print(TEXT_PAID)
    print("-" * 60)
    print()
    confirmation = input("Запустить broadcast? (yes/no): ").strip().lower()
    if confirmation not in ("yes", "y", "да", "д"):
        print("Отменено.")
        await engine.dispose()
        return

    bot = Bot(token=BOT_TOKEN)
    all_stats = []
    try:
        stats_paid = await send_broadcast(bot, paid_users, TEXT_PAID, "Pro/Grandfather")
        all_stats.append(stats_paid)

        stats_free = await send_broadcast(bot, free_users, TEXT_FREE, "Free")
        all_stats.append(stats_free)
    finally:
        await bot.session.close()
        await engine.dispose()

    print()
    print("=" * 60)
    print("ИТОГО")
    print("=" * 60)
    total_sent = sum(s["sent"] for s in all_stats)
    total_blocked = sum(s["blocked"] for s in all_stats)
    total_errors = sum(s["errors"] for s in all_stats)
    for s in all_stats:
        print(f"  {s['segment']}: отправлено {s['sent']}, блок {s['blocked']}, ошибки {s['errors']}")
    print(f"  ВСЕГО: отправлено {total_sent}, блок {total_blocked}, ошибки {total_errors}")


if __name__ == "__main__":
    asyncio.run(main())