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
    "Что я делаю:\n\n"
    "• Ищу вакансии в 100+ источниках — карьерные сайты, Telegram-каналы, международные компании\n"
    "• Присылаю до 3 подходящих вакансий в день\n"
    "• По каждой могу разобрать соответствие — что совпало с профилем, чего не хватает, стоит ли откликаться\n"
    "• Собираю персональное резюме под вакансию (до 2 в день)\n"
    "• Пишу сопроводительное письмо (до 2 в день)\n\n"
    "<b>Два тарифа:</b>\n\n"
    "⚡️ <b>349₽ / неделя</b> — попробовать быстро.\n\n"
    "🔁 <b>990₽ / месяц</b> — если настроен на поиск серьёзно.\n\n"
    "Оплата картой любого банка. Автопродление можно отключиь в любой момент. После оплаты попадёшь в мой канал @{channel} — так работает Tribute. Там я делюсь апдейтами по боту и заметками про поиск работы. Не выходи из канала — иначе подписка может отключиться."
)


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message) -> None:
    log.info("upgrade_opened", telegram_id=message.from_user.id)
    user_id = message.from_user.id

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

    # 9.1 — Grandfather получает доступ без подписки
    if user and user.plan == "grandfather":
        await message.answer(
            "Тебе не нужно платить — у тебя есть доступ ко всем функциям бота "
            "как у пользователя раннего тестирования.\n\n"
            "Если что-то не работает или хочется новую фичу — "
            "напиши мне напрямую @puniapple"
        )
        return
    
    # Если уже подписан — показываем статус, не предлагаем покупать снова
    if user and user.plan == "pro" and user.subscription_status == "pro_active":
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
        text = (
            f"💎 Подписка активна до {expires}.\n\n"
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
            text="⚡️ 349₽ / неделя",
            url=settings.tribute_subscription_weekly_url,
        )],
        [InlineKeyboardButton(
            text="🔁 990₽ / месяц",
            url=settings.tribute_subscription_monthly_url,
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
    log.info("cancel_opened", telegram_id=message.from_user.id)
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
    log.info("my_plan_opened", telegram_id=message.from_user.id)
    user_id = message.from_user.id
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()
    if not user:
        await message.answer("Я тебя ещё не знаю. /start чтобы начать.")
        return

    # Grandfather — доступ без подписки
    if user.plan == "grandfather":
        await message.answer(
            "У тебя есть <b>полный доступ 💎</b>\n\n"
            "Бесплатно и бессрочно — как у пользователя раннего тестирования.\n\n"
            "— До 3 подходящих вакансий в день\n"
            "— До 2 разборов соответствия в день\n"
            "— До 2 сопроводительных в день\n"
            "— До 2 резюме под вакансию в день\n\n"
            "Спасибо, что был со мной с самого начала.",
            parse_mode="HTML",
        )
        return

    # Активная подписка (weekly или monthly)
    if user.subscription_status == "pro_active":
        is_weekly = False
        if user.last_payment_at and user.plan_expires_at:
            period_days = (user.plan_expires_at - user.last_payment_at).days
            is_weekly = period_days < 20

        period_word = "неделя" if is_weekly else "месяц"
        amount = "349₽" if is_weekly else "990₽"
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"

        await message.answer(
            f"Подписка активна ({period_word})\n\n"
            f"— До 3 подходящих вакансий в день\n"
            f"— До 2 разборов соответствия в день\n"
            f"— До 2 сопроводительных в день\n"
            f"— До 2 резюме под вакансию в день\n\n"
            f"Следующее списание: {expires} ({amount})\n\n"
            f"Отменить подписку: /cancel_subscription",
            parse_mode="HTML",
        )
        return

    # Подписка отменена, но ещё действует до expiry
    if user.subscription_status == "pro_cancelled_until_expiry":
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
        await message.answer(
            f"Подписка отменена, но ещё действует до {expires}.\n\n"
            f"После этой даты доступ к боту закроется.\n\n"
            f"Передумал? /upgrade",
            parse_mode="HTML",
        )
        return

    # Подписка истекла
    if user.subscription_status == "pro_expired":
        expires = user.plan_expires_at.strftime("%d.%m.%Y") if user.plan_expires_at else "—"
        await message.answer(
            f"Подписка истекла {expires}.\n\n"
            f"Возобновить доступ: /upgrade",
            parse_mode="HTML",
        )
        return

    # Всё остальное — юзер без подписки (бывший free)
    await message.answer(
        "У тебя пока нет подписки.\n\n"
        "Бот работает по подписке от 349₽ в неделю.\n\n"
        "Подключить: /upgrade",
        parse_mode="HTML",
    )