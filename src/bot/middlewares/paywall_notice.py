"""Middleware: paywall notice для неактивных Free.

Один раз при первом взаимодействии показывает текст «бот теперь работает
по подписке», снимает флаг, показывает paywall с двумя кнопками.

Не проверяет has_access() на каждый запрос — эта задача handler'ов
там, где действие требует доступа. Middleware только про one-shot
уведомление о переходе на платную модель.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import select

from src.config import settings
from src.db.models import User
from src.db.session import async_session

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Тексты (из ТЗ 3.2)
# ─────────────────────────────────────────────────────────────
NOTICE_TEXT = (
    "Привет! Теперь бот работает только по подписке. "
    "Запустить поиск можно от 349₽ 👇🏼"
)

PAYWALL_TITLE = "Выбирай тариф:"


def _paywall_keyboard() -> InlineKeyboardMarkup:
    """Две URL-кнопки на Tribute (weekly + monthly)."""
    weekly_url = settings.tribute_subscription_weekly_url
    monthly_url = settings.tribute_subscription_monthly_url

    buttons = []
    if weekly_url:
        buttons.append([
            InlineKeyboardButton(text="📅 Неделя — 349₽", url=weekly_url)
        ])
    if monthly_url:
        buttons.append([
            InlineKeyboardButton(text="💎 Месяц — 990₽", url=monthly_url)
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


class PaywallNoticeMiddleware(BaseMiddleware):
    """Показывает one-shot paywall-уведомление тем, кто пометен флагом."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Определяем telegram_id + куда отвечать
        telegram_id = None
        chat_id = None
        bot = data.get("bot")

        if isinstance(event, Message):
            if event.from_user:
                telegram_id = event.from_user.id
                chat_id = event.chat.id
        elif isinstance(event, CallbackQuery):
            if event.from_user:
                telegram_id = event.from_user.id
                if event.message:
                    chat_id = event.message.chat.id

        # Не наш случай — пропускаем
        if telegram_id is None or chat_id is None or bot is None:
            return await handler(event, data)

        # Смотрим юзера в БД
        async with async_session() as session:
            user = (await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )).scalar_one_or_none()

            # Юзера ещё нет — /start сам его создаст, ничего не делаем
            if user is None:
                return await handler(event, data)

            # Нет флага — просто пропускаем дальше
            if not user.pending_paywall_notice:
                return await handler(event, data)

            # Флаг стоит — показываем one-shot уведомление и снимаем
            try:
                await bot.send_message(chat_id=chat_id, text=NOTICE_TEXT)
                await bot.send_message(
                    chat_id=chat_id,
                    text=PAYWALL_TITLE,
                    reply_markup=_paywall_keyboard(),
                )
            except TelegramAPIError as e:
                logger.warning(
                    "paywall_notice_send_failed",
                    extra={"telegram_id": telegram_id, "error": str(e)},
                )
                # Если не смогли отправить — не снимаем флаг, попробуем позже
                return None

            # Снимаем флаг — показываем только один раз
            user.pending_paywall_notice = False
            await session.commit()

            # Если это был callback — ответим, чтобы не висел "loading"
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except TelegramAPIError:
                    pass

            # НЕ вызываем handler — юзер должен сначала прочитать paywall
            return None
