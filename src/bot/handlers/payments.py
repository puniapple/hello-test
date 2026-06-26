"""Bot handlers для платных команд: /upgrade, /cancel_subscription, /subscription_status."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from src.config import settings
from src.db.models import User
from src.db.session import async_session
from src.services.tribute import TributeError, get_tribute_client

log = structlog.get_logger(__name__)
router = Router()


# --- /upgrade ---


UPGRADE_INTRO_TEXT = (
    "💎 <b>Pro подписка</b> — что получаешь:\n\n"
    "• 3 цикла подбора в день вместо одного\n"
    "• До 8 вакансий за цикл вместо 3\n"
    "• Приоритет в очереди матчинга\n\n"
    "<b>Два варианта оплаты:</b>\n\n"
    "🔁 <b>Подписка</b> — 990₽/мес, карта любого банка, "
    "продлевается автоматически, отмена в любой момент через /cancel_subscription\n\n"
    "1️⃣ <b>Разово на 30 дней</b> — 990₽ одним платежом, картой или через СБП, "
    "без привязки карты. По истечении нужно купить снова."
)


def _upgrade_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔁 Подписка 990₽/мес (автопродление)",
            callback_data="upgrade:sub",
        )],
        [InlineKeyboardButton(
            text="1️⃣ Разово 990₽ на 30 дней (карта или СБП)",
            callback_data="upgrade:once",
        )],
    ])


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
        plan_type = "подписка" if user.auto_renew else "разовый Pro"
        text = f"💎 У тебя уже активный {plan_type} до {expires}."
        if user.auto_renew:
            text += "\n\nЕсли хочешь отменить автопродление — /cancel_subscription."
        else:
            text += "\n\nКогда срок закончится, сможешь купить снова через /upgrade."
        await message.answer(text)
        return

    await message.answer(
        UPGRADE_INTRO_TEXT,
        reply_markup=_upgrade_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("upgrade:"))
async def cb_upgrade_choice(callback: CallbackQuery) -> None:
    choice = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    bot = callback.bot

    if choice == "sub":
        amount = settings.pro_price_kopeks
        period = settings.pro_period_recurring
        title = "Pro подписка на FindFcknJobBot"
        description = "3 цикла подбора в день, до 8 вакансий за цикл. Автопродление каждый месяц."
        hint = "Карта любого банка. После оплаты подписка продлевается автоматически каждый месяц."
    elif choice == "once":
        amount = settings.pro_onetime_price_kopeks
        period = settings.pro_period_onetime
        title = "Pro на 30 дней — FindFcknJobBot"
        description = "3 цикла подбора в день, до 8 вакансий за цикл. Разовый платёж на 30 дней."
        hint = "Можно картой или через СБП. Привязки карты не будет — через 30 дней нужно будет купить снова."
    else:
        await callback.answer("Неизвестный вариант")
        return

    me = await bot.get_me()

    try:
        client = get_tribute_client()
        order = await client.create_order(
            amount=amount,
            currency="rub",
            title=title,
            description=description,
            period=period,
            customer_id=str(user_id),
            success_url=f"https://t.me/{me.username}?start=paid",
            fail_url=f"https://t.me/{me.username}?start=fail",
        )
    except TributeError as e:
        log.error("create_order_failed", user=user_id, choice=choice, error=str(e))
        await callback.message.answer(
            "Что-то у меня не получилось создать счёт. Попробуй через пару минут — "
            "если не пройдёт, напиши @puniapple."
        )
        await callback.answer()
        return

    payment_url = order.get("paymentUrl") or order.get("webappPaymentUrl")
    if not payment_url:
        log.error("no_payment_url_in_order", order=order)
        await callback.message.answer(
            "Tribute не вернул ссылку для оплаты. Напиши мне @puniapple, разберёмся."
        )
        await callback.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)
    ]])
    await callback.message.answer(
        f"Оплати по ссылке ниже.\n{hint}",
        reply_markup=kb,
    )
    await callback.answer()


# --- /cancel_subscription ---


@router.message(Command("cancel_subscription"))
async def cmd_cancel_subscription(message: Message) -> None:
    user_id = message.from_user.id

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

    if not user or not user.tribute_order_uuid:
        await message.answer("У тебя нет активной подписки. /upgrade чтобы оформить.")
        return

    # Разовый Pro — отменять нечего
    if not user.auto_renew:
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
        await message.answer(
            f"У тебя разовый Pro до {expires}, отменять нечего — карта не привязана, "
            "автосписаний не будет. Когда срок закончится, можно купить снова через /upgrade."
        )
        return

    # Уже отменена
    if user.subscription_status not in ("pro_active", "pro_grace"):
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
        await message.answer(
            f"Подписка уже отменена. Доступ останется до {expires}."
        )
        return

    # Отменяем через Tribute API
    try:
        client = get_tribute_client()
        await client.cancel_order(user.tribute_order_uuid)
    except TributeError as e:
        log.error("cancel_order_failed", user=user_id, error=str(e))
        await message.answer(
            "Не получилось отменить через Tribute API. Попробуй ещё раз или напиши @puniapple."
        )
        return

    expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
    await message.answer(
        f"Подписка отменена. Pro останется активным до {expires} — "
        "потом бот вернётся в бесплатный режим. Будем скучать."
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
    plan_type = "Подписка с автопродлением" if user.auto_renew else "Разовый Pro на 30 дней"
    status_human = {
        "pro_active": "активна",
        "pro_grace": "проблема со списанием, Tribute ретраит",
        "pro_cancelled_until_expiry": "отменена, доступ до конца оплаченного периода",
        "pro_expired": "истекла",
    }.get(user.subscription_status, user.subscription_status)

    msg = f"💎 Pro\n{plan_type}\nДействует до: {expires}\nСтатус: {status_human}"

    # Для разовых юзеров с приближающимся окончанием — подсказка про продление
    if (
        not user.auto_renew
        and user.subscription_status == "pro_active"
        and user.plan_expires_at
    ):
        days_left = (user.plan_expires_at - datetime.now(timezone.utc)).days
        if 0 < days_left <= 7:
            msg += f"\n\n⏰ Осталось {days_left} дн. /upgrade чтобы продлить."

    await message.answer(msg)