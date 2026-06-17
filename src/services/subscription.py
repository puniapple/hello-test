"""Проверка подписки юзера на обязательный канал."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from src.config import settings

logger = logging.getLogger(__name__)

# Статусы участника, которые считаются "подписан"
SUBSCRIBED_STATUSES = {"member", "administrator", "creator"}


def is_required_channel_configured() -> bool:
    """True если в env задан канал — значит надо проверять подписку."""
    return bool(settings.required_channel_username.strip())


def _is_admin(telegram_id: int) -> bool:
    if not settings.admin_telegram_ids:
        return False
    ids = {
        int(x.strip()) for x in settings.admin_telegram_ids.split(",") if x.strip()
    }
    return telegram_id in ids


async def is_subscribed(bot: Bot, telegram_id: int) -> bool:
    """Проверка подписки на REQUIRED_CHANNEL_USERNAME.

    - Если канал не настроен в env — True для всех (gate выключен)
    - Если юзер админ бота — True (без проверки)
    - Иначе обращается к Telegram API
    """
    if not is_required_channel_configured():
        return True
    if _is_admin(telegram_id):
        return True

    channel = settings.required_channel_username.strip()
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=telegram_id)
        return member.status in SUBSCRIBED_STATUSES
    except TelegramAPIError as e:
        # Если бот не добавлен в канал админом — будет ошибка.
        # Лучше пропускать юзера (не блокировать ошибкой нашей инфры).
        logger.warning(
            "subscription_check_failed",
            extra={"telegram_id": telegram_id, "channel": channel, "error": str(e)},
        )
        return True


def get_channel_url() -> str:
    """URL для кнопки 'Перейти в канал'."""
    channel = settings.required_channel_username.strip().lstrip("@")
    return f"https://t.me/{channel}"


def get_channel_display() -> str:
    """Отображаемое имя канала для текстов."""
    channel = settings.required_channel_username.strip()
    if not channel.startswith("@"):
        channel = "@" + channel
    return channel