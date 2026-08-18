"""Broadcast о переходе на платную модель — активным Free-юзерам.

Отправляет одноразовое сообщение тем, кто получал вакансии за последние 4 дня
и всё ещё на plan='free'. Grandfather и pro не трогаем.

Также ставит флаг pending_paywall_notice = true у 69 неактивных Free —
чтобы при их следующем взаимодействии middleware показал paywall.

DRY_RUN режим — сначала показывает preview, потом ждёт подтверждения.

Запуск: python3 broadcast_paywall_active.py
Лежит в .gitignore, коммитить не надо.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import distinct, select, update

from src.config import settings
from src.db.models import User, VacancyMatch
from src.db.session import async_session, engine

BOT_TOKEN = settings.telegram_bot_token
if not BOT_TOKEN:
    raise RuntimeError("telegram_bot_token не задан в settings")

DELAY_BETWEEN_SENDS_SEC = 0.5
ACTIVE_WINDOW_DAYS = 4


BROADCAST_TEXT = (
    "Привет! Теперь бот работает по подписке.\n\n"
    "Если хочешь каждый день получать вакансии, видеть оценку соответствия твоему запросу, собирать готовое резюме и сопроводительное письмо — подключи подписку по кнопке ниже.\n\n"
    "На неделю — 349₽, на месяц — 990₽. Можно отменить в любой момент."
)


def paywall_keyboard() -> InlineKeyboardMarkup:
    """Две URL-кнопки на Tribute."""
    buttons = []
    if settings.tribute_subscription_weekly_url:
        buttons.append([InlineKeyboardButton(
            text="⚡️ Неделя — 349₽",
            url=settings.tribute_subscription_weekly_url,
        )])
    if settings.tribute_subscription_monthly_url:
        buttons.append([InlineKeyboardButton(
            text="🔁 Месяц — 990₽",
            url=settings.tribute_subscription_monthly_url,
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def collect_active_free() -> list[User]:
    """Free-юзеры с доставкой за последние 4 дня."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVE_WINDOW_DAYS)
    async with async_session() as s:
        subq = (
            select(distinct(VacancyMatch.user_id))
            .where(VacancyMatch.delivered_at >= cutoff)
        ).subquery()
        result = await s.execute(
            select(User)
            .where(User.plan == "free")
            .where(User.is_active.is_(True))
            .where(User.id.in_(select(subq)))
        )
        return list(result.scalars())


async def collect_inactive_free() -> list[User]:
    """Free-юзеры БЕЗ доставки за последние 4 дня — им флаг pending_paywall_notice."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVE_WINDOW_DAYS)
    async with async_session() as s:
        subq_recent = (
            select(distinct(VacancyMatch.user_id))
            .where(VacancyMatch.delivered_at >= cutoff)
        ).subquery()
        result = await s.execute(
            select(User)
            .where(User.plan == "free")
            .where(User.is_active.is_(True))
            .where(~User.id.in_(select(subq_recent)))
        )
        return list(result.scalars())


async def mark_inactive_free_with_flag(users: list[User]) -> int:
    """Проставить pending_paywall_notice=true всем указанным юзерам."""
    if not users:
        return 0
    ids = [u.id for u in users]
    async with async_session() as s:
        result = await s.execute(
            update(User)
            .where(User.id.in_(ids))
            .values(pending_paywall_notice=True)
        )
        await s.commit()
        return result.rowcount or 0


async def send_broadcast(bot: Bot, users: list[User]) -> dict:
    stats = {"total": len(users), "sent": 0, "blocked": 0, "errors": 0}
    if not users:
        print("Никого нет для broadcast.")
        return stats

    kb = paywall_keyboard()
    print(f"\n=== Отправка {len(users)} активным Free ===")

    for i, user in enumerate(users, 1):
        username = f"@{user.telegram_username}" if user.telegram_username else "—"
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=BROADCAST_TEXT,
                reply_markup=kb,
            )
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
    active = await collect_active_free()
    inactive = await collect_inactive_free()

    now_utc = datetime.now(timezone.utc)
    print("=" * 60)
    print("BROADCAST — переход на платную модель")
    print(f"Сейчас: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    print(f"Активные Free (получат сразу):      {len(active)}")
    print(f"Неактивные Free (флаг на будущее):  {len(inactive)}")
    print()
    print("Текст broadcast:")
    print("-" * 60)
    print(BROADCAST_TEXT)
    print("-" * 60)
    print()
    print(f"Кнопки: weekly URL={bool(settings.tribute_subscription_weekly_url)}, "
          f"monthly URL={bool(settings.tribute_subscription_monthly_url)}")
    print()

    if not active and not inactive:
        print("Никого нет в выборках. Завершаю.")
        await engine.dispose()
        return

    confirmation = input(
        "Запустить обе операции — broadcast активным + флаг неактивным? (yes/no): "
    ).strip().lower()
    if confirmation not in ("yes", "y", "да", "д"):
        print("Отменено.")
        await engine.dispose()
        return

    bot = Bot(token=BOT_TOKEN)
    try:
        # Сначала broadcast активным (они самые важные)
        stats = await send_broadcast(bot, active)

        # Потом флаг неактивным
        marked = await mark_inactive_free_with_flag(inactive)
        print(f"\n=== Флаг pending_paywall_notice проставлен {marked} неактивным ===")
    finally:
        await bot.session.close()
        await engine.dispose()

    print()
    print("=" * 60)
    print("ИТОГО")
    print("=" * 60)
    print(f"  Активным Free отправлено: {stats['sent']}")
    print(f"  Блок:                     {stats['blocked']}")
    print(f"  Ошибки:                   {stats['errors']}")
    print(f"  Неактивным флаг:          {marked}")


if __name__ == "__main__":
    asyncio.run(main())
