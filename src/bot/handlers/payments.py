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
    "💎 <b>Pro подписка</b> — что получаешь:\n\n"
    "• Несколько подборок вакансий в день\n"
    "• До 8 вакансий за подборку\n"
    "• Приоритет в очереди матчинга\n\n"
    "990₽/мес. Оплата картой любого банка. "
    "Продлевается автоматически, отменить можно в любой момент — "
    "напишу как это сделать когда понадобится.\n\n"
    "После оплаты попадёшь в мой канал @{channel} — так работает Tribute. "
    "Там технически подтверждается что у тебя активная подписка. "
    "Постов там почти нет, можно не заглядывать."
)


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message) -> None:
    user_id = message.from_user.id

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

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
    if not settings.tribute_subscription_url:
        log.error("tribute_subscription_url_not_configured")
        await message.answer(
            "У меня тут что-то не настроено с оплатой. Напиши @puniapple, разберёмся."
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="💳 Оформить Pro за 990₽/мес",
            url=settings.tribute_subscription_url,
        )
    ]])
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


@router.message(Command("subscription_status"))
async def cmd_subscription_status(message: Message) -> None:
    user_id = message.from_user.id

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

    if not user:
        await message.answer("Я тебя ещё не знаю. /start чтобы начать.")
        return

    if user.plan != "pro":
        await message.answer("Сейчас у тебя бесплатный план. /upgrade чтобы получить Pro.")
        return

    expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
    status_human = {
        "pro_active": "активна, автопродление включено",
        "pro_cancelled_until_expiry": "отменена, доступ до конца оплаченного периода",
        "pro_expired": "истекла",
    }.get(user.subscription_status, user.subscription_status)

    msg = f"💎 Pro\nДействует до: {expires}\nСтатус: {status_human}"
    await message.answer(msg)
