"""Subscription check + notification service.

Логика:
1. Pro и Grandfather юзеры освобождены от подписки на канал.
2. In-memory кеш TTL 1 час — не долбим Telegram API каждый цикл.
3. Функция notify_if_unsubscribed — атомарно проверяет, уведомляет и метит,
   чтобы не спамить юзера в каждом цикле.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from src.config import settings
from src.db.models import User
from src.db.session import async_session

logger = logging.getLogger(__name__)

SUBSCRIBED_STATUSES = {"creator", "administrator", "member"}

# Пригласительная ссылка на канал «Можно иначе» — канал приватный,
# поэтому не выводится через get_channel_url() (тот возвращает t.me/handle
# из env REQUIRED_CHANNEL_USERNAME).
CHANNEL_INVITE_LINK = "https://t.me/+O4j3RGUm50NjMmIy"
CHANNEL_DISPLAY_NAME = "«Можно иначе»"

# Как часто повторно уведомлять юзера об отписке
GATE_NOTIFY_COOLDOWN = timedelta(days=7)

# In-memory кеш: telegram_id → (is_subscribed, checked_at)
_subscription_cache: dict[int, tuple[bool, datetime]] = {}
CACHE_TTL = timedelta(hours=1)


def is_required_channel_configured() -> bool:
    """True если в env задан канал — значит надо проверять подписку."""
    return bool(settings.required_channel_username.strip())


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


def _is_admin(telegram_id: int) -> bool:
    if not settings.admin_telegram_ids:
        return False
    ids = {
        int(x.strip()) for x in settings.admin_telegram_ids.split(",") if x.strip()
    }
    return telegram_id in ids


async def _is_paid_user(telegram_id: int) -> bool:
    """Догружаем юзера из БД и проверяем тариф.

    Pro и Grandfather освобождены от требования подписки на канал.
    """
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
    if user is None:
        return False
    plan = (user.plan or "free").lower()
    return plan in ("pro", "grandfather")


async def is_subscribed(bot: Bot, telegram_id: int, use_cache: bool = True) -> bool:
    """Проверка подписки на REQUIRED_CHANNEL_USERNAME.

    Порядок проверок (короткое замыкание, чтобы не делать лишних запросов):
    - Если канал не настроен в env — True для всех (gate выключен)
    - Если юзер админ бота — True (без проверки)
    - Если юзер на Pro или Grandfather — True (без проверки)
    - Если есть свежий кеш (< 1 часа) — вернуть из кеша
    - Иначе обращается к Telegram API и кеширует результат
    """
    if not is_required_channel_configured():
        return True
    if _is_admin(telegram_id):
        return True

    if await _is_paid_user(telegram_id):
        return True

    # Кеш
    if use_cache:
        cached = _subscription_cache.get(telegram_id)
        if cached is not None:
            status, checked_at = cached
            if datetime.now(timezone.utc) - checked_at < CACHE_TTL:
                return status

    # Реальная проверка через Telegram API
    channel = settings.required_channel_username.strip()
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=telegram_id)
        subscribed = member.status in SUBSCRIBED_STATUSES
    except TelegramAPIError as e:
        logger.warning(
            "subscription_check_failed",
            extra={"telegram_id": telegram_id, "channel": channel, "error": str(e)},
        )
        # Если бот не добавлен в канал админом — не блокируем юзера
        return True

    # Сохраняем в кеш
    _subscription_cache[telegram_id] = (subscribed, datetime.now(timezone.utc))
    return subscribed


def invalidate_subscription_cache(telegram_id: int) -> None:
    """Сбросить кеш подписки для конкретного юзера.

    Вызывается когда юзер жмёт "Я подписался" — надо переспросить у Telegram
    и не отдать закешированный False.
    """
    _subscription_cache.pop(telegram_id, None)


def _build_gate_notification_kb() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой Проверить подписку — та же, что в _send_subscription_gate."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check")]
        ]
    )


def _build_unsubscribe_message() -> str:
    """Текст сообщения при обнаружении отписки во время цикла матчинга."""
    return (
        f'Ты отписался от канала <a href="{CHANNEL_INVITE_LINK}">{CHANNEL_DISPLAY_NAME}</a>, '
        f'подпишись и я продолжу присылать тебе вакансии.'
    )


async def notify_if_unsubscribed(bot: Bot, user: User) -> bool:
    """Отправляет юзеру одноразовое сообщение об отписке.

    Возвращает True если сообщение было отправлено (или уже было отправлено недавно).
    Возвращает False если юзер подписан либо уведомление не требуется.

    Логика:
    - Если юзер подписан → сбрасываем gate_notified_at (готовы уведомить снова)
    - Если не подписан + gate_notified_at пустое ИЛИ старше 7 дней → шлём + метим
    - Если не подписан + gate_notified_at свежий → молчим
    """
    # Проверяем текущий статус (использует кеш)
    subscribed = await is_subscribed(bot, user.telegram_id)

    async with async_session() as session:
        # Свежая копия юзера — читаем/пишем в одной сессии
        result = await session.execute(
            select(User).where(User.id == user.id)
        )
        db_user = result.scalar_one_or_none()
        if db_user is None:
            return False

        if subscribed:
            # Юзер вернулся — сбрасываем метку, готовы уведомить снова если снова отпишется
            if db_user.gate_notified_at is not None:
                db_user.gate_notified_at = None
                await session.commit()
            return False

        # Не подписан. Проверяем cooldown.
        now = datetime.now(timezone.utc)
        if (
            db_user.gate_notified_at is not None
            and now - db_user.gate_notified_at < GATE_NOTIFY_COOLDOWN
        ):
            return True  # уже уведомили недавно, молчим

        # Шлём сообщение
        try:
            await bot.send_message(
                chat_id=db_user.telegram_id,
                text=_build_unsubscribe_message(),
                reply_markup=_build_gate_notification_kb(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramAPIError as e:
            logger.warning(
                "unsubscribe_notify_failed",
                extra={"telegram_id": db_user.telegram_id, "error": str(e)},
            )
            return False

        # Метим факт уведомления
        db_user.gate_notified_at = now
        await session.commit()
        return True