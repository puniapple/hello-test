"""Shutdown middleware — показывает финальное сообщение и снимает флаг.

Заменяет PaywallNoticeMiddleware. Логика:
- При любом взаимодействии юзера (сообщение или callback)
- Если shutdown_notified_at IS NULL — показывает финальное сообщение, ставит время
- Иначе (юзер уже видел сообщение) — молчит, ничего не делает и не пропускает дальше

Никаких кнопок, никакой генерации. Просто финальный текст.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select

from src.db.models import User
from src.db.session import async_session

logger = logging.getLogger(__name__)


SHUTDOWN_TEXT = (
    "Привет! Этот бот больше не работает.\n\n"
    "Если у тебя есть вопрос или нужна помощь в поиске работы — напиши @puniapple"
)


class ShutdownMiddleware(BaseMiddleware):
    """Показывает финальное сообщение раз и снимает флаг. Дальше молчит."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
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

        # Не наш случай — молча пропускаем без вызова handler'а (бот в shutdown)
        if telegram_id is None or chat_id is None or bot is None:
            return None

        async with async_session() as session:
            user = (await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )).scalar_one_or_none()

            # Юзера нет в БД — это первое взаимодействие. Показываем финальное сообщение.
            # НЕ создаём запись в users (бот закрыт, не нужно расширять базу).
            if user is None:
                try:
                    await bot.send_message(chat_id=chat_id, text=SHUTDOWN_TEXT)
                except TelegramAPIError as e:
                    logger.warning(
                        "shutdown_notice_send_failed_new_user",
                        extra={"telegram_id": telegram_id, "error": str(e)},
                    )
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except TelegramAPIError:
                        pass
                return None

            # Юзер уже видел shutdown — молчим
            if user.shutdown_notified_at is not None:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except TelegramAPIError:
                        pass
                return None

            # Показываем финальное сообщение и ставим время
            try:
                await bot.send_message(chat_id=chat_id, text=SHUTDOWN_TEXT)
            except TelegramAPIError as e:
                logger.warning(
                    "shutdown_notice_send_failed",
                    extra={"telegram_id": telegram_id, "error": str(e)},
                )
                # Если не смогли отправить — не помечаем, попробуем при следующем взаимодействии
                return None

            user.shutdown_notified_at = datetime.now(timezone.utc)
            await session.commit()

            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except TelegramAPIError:
                    pass

            # НЕ вызываем handler — бот закрыт, никакая логика не должна работать
            return None

