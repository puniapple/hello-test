"""Bot handlers для платных команд: /upgrade, /cancel_subscription, /subscription_status.

Использует Tribute Subscriptions API — юзер оплачивает через прямую ссылку,
Tribute автоматически добавляет его в приватный канал и шлёт webhook боту.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from src.config import settings
from src.db.models import User
from src.db.session import async_session

log = structlog.get_logger(__name__)
router = Router()


# --- /upgrade ---


UPGRADE_TEXT = (
    "💎 <b>Pro</b> — что получаешь:\n\n"
    "• Несколько подборок вакансий в день вместо одной\n"
    "• До 5 вакансий за подборку вместо 3\n"
    "• До 5 сопроводительных в день вместо 1\n\n"
    "<b>Два варианта оплаты:</b>\n\n"
    "🔁 <b>990₽ / месяц</b> — продлевается автоматически. "
    "Для тех кто настроен искать обстоятельно.\n\n"
    "⚡️ <b>349₽ / неделя</b> — тоже с автопродлением, можно отменить в любой момент. "
    "Для тех кто хочет быстро попробовать.\n\n"
    "Оплата картой любого банка. После оплаты попадёшь в мой канал @{channel} — "
    "так работает Tribute. Там я делюсь апдейтами по боту и заметками о поиске работы. "
    "Не выходи из канала — подписка может отключиться. "
)


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message) -> None:
    user_id = message.from_user.id

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

    # 9.1 — Grandfather не платит, у него Pro навсегда
    if user and user.plan == "grandfather":
        await message.answer(
            "Тебе не нужно платить — у тебя Pro 💎 бесплатно навсегда "
            "как у пользователя раннего тестирования.\n\n"
            "Если что-то не работает или хочется новую фичу — "
            "напиши мне напрямую @puniapple"
        )
        return
    
    # Если уже Pro — показываем статус, не предлагаем покупать снова
    if user and user.plan == "pro" and user.subscription_status == "pro_active":
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
        text = (
            f"💎 У тебя уже активный Pro до {expires}.\n\n"
            "Если хочешь отменить автопродление — /cancel_subscription."
        )
        await message.answer(text)
        return

    # Проверка что ссылка на оплату сконфигурирована
    if not settings.tribute_subscription_monthly_url or not settings.tribute_subscription_weekly_url:
        log.error(
            "tribute_subscription_urls_not_configured",
            monthly=bool(settings.tribute_subscription_monthly_url),
            weekly=bool(settings.tribute_subscription_weekly_url),
        )
        await message.answer(
            "У меня тут что-то не настроено с оплатой. Напиши @puniapple, разберёмся."
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔁 990₽ / месяц",
            url=settings.tribute_subscription_monthly_url,
        )],
        [InlineKeyboardButton(
            text="⚡️ 349₽ / неделя",
            url=settings.tribute_subscription_weekly_url,
        )],
    ])
    await message.answer(
        UPGRADE_TEXT.format(channel=settings.tribute_channel_username),
        reply_markup=kb,
        parse_mode="HTML",
    )


# --- /cancel_subscription ---


CANCEL_INSTRUCTIONS = (
    "Чтобы отменить подписку:\n\n"
    "1. Открой @tribute\n"
    "2. Нажми меню (⋯) → «Подписки» или «Мои подписки»\n"
    "3. Найди подписку на @{channel}\n"
    "4. Нажми «Отменить»\n\n"
    "Доступ к Pro останется до конца оплаченного периода — я узнаю об отмене "
    "автоматически и сохраню Pro до {expires}. Дальше вернёшься на бесплатный план."
)


@router.message(Command("cancel_subscription"))
async def cmd_cancel_subscription(message: Message) -> None:
    user_id = message.from_user.id

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

    if not user or not user.tribute_subscription_id:
        await message.answer("У тебя нет активной подписки. /upgrade чтобы оформить.")
        return

    if user.subscription_status == "pro_cancelled_until_expiry":
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
        await message.answer(
            f"Подписка уже отменена. Доступ останется до {expires}."
        )
        return

    if user.subscription_status != "pro_active":
        await message.answer(
            "У тебя нет активной подписки. /upgrade чтобы оформить."
        )
        return

    expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
    await message.answer(
        CANCEL_INSTRUCTIONS.format(
            channel=settings.tribute_channel_username,
            expires=expires,
        )
    )


# --- /subscription_status ---


@router.message(Command("my_plan"))
async def cmd_my_plan(message: Message) -> None:
    user_id = message.from_user.id
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()
    if not user:
        await message.answer("Я тебя ещё не знаю. /start чтобы начать.")
        return

    # 5.3 — Grandfather
    if user.plan == "grandfather":
        await message.answer(
            "Твой тариф: <b>Pro 💎 ∞</b>\n\n"
            "Бесплатный пожизненный доступ — как у пользователя раннего тестирования.\n\n"
            "— Несколько подборок в день\n"
            "— До 5 вакансий за раз\n"
            "— До 5 сопроводительных в день\n\n"
            "Спасибо, что был со мной с самого начала.",
            parse_mode="HTML",
        )
        return

    # 5.1 — Free
    if user.plan != "pro":
        await message.answer(
            "Твой тариф: <b>Free</b>.\n\n"
            "— 1 подборка в день\n"
            "— До 3 вакансий за раз\n"
            "— 1 сопроводительное в день\n\n"
            "Хочешь больше? /upgrade",
            parse_mode="HTML",
        )
        return

    # 5.2 — Pro: определяем период по разнице (expires - last_payment)
    is_weekly = False
    if user.last_payment_at:
        period_days = (user.plan_expires_at - user.last_payment_at).days
        is_weekly = period_days < 20
    
    period_word = "неделя" if is_weekly else "месяц"
    amount = "349₽" if is_weekly else "990₽"
    expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"

    if user.subscription_status == "pro_active":
        msg = (
            f"Твой тариф: <b>Pro 💎</b> ({period_word})\n\n"
            f"— Несколько подборок в день\n"
            f"— До 5 вакансий за раз\n"
            f"— До 5 сопроводительных в день\n"
            f"— Следующее списание: {expires} ({amount})\n\n"
            f"Отменить подписку: /cancel_subscription"
        )
    elif user.subscription_status == "pro_cancelled_until_expiry":
        msg = (
            f"Твой тариф: <b>Pro 💎</b> (отменена)\n\n"
            f"Действует до: {expires}\n"
            f"Дальше — переход на Free.\n\n"
            f"Передумал? /upgrade"
        )
    elif user.subscription_status == "pro_expired":
        msg = (
            f"Твой Pro истёк {expires}.\n\n"
            f"Сейчас ты на Free.\n\n"
            f"Вернуть Pro: /upgrade"
        )
    else:
        msg = f"💎 Pro\nДействует до: {expires}\nСтатус: {user.subscription_status}"

    await message.answer(msg, parse_mode="HTML")
