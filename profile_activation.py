"""Auto-activation of job search when profile becomes ready.

Юзер разговаривает с ProfileAgent через любой канал (текст, голос, CV).
После каждого обновления профиля проверяем: готов ли для поиска?
Если да — активируем поиск автоматически, не дожидаясь нажатия кнопки.

Функция идемпотентна: если юзер уже активен, ничего не делает.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from sqlalchemy import select

from src.db.models import Profile, User
from src.db.session import async_session
from src.services.profile_validation import is_profile_ready

logger = logging.getLogger(__name__)


def _build_activation_text(user: User) -> str:
    """Тот же текст, что в profile_edit.py handle_start_search — один источник правды."""
    is_paid = user.plan == "grandfather" or (
        user.plan == "pro" and user.subscription_status == "pro_active"
    )
    if is_paid:
        return (
            "Готово, ты в поиске 🚀\n\n"
            "Я буду присылать подборки несколько раз в день — до 5 самых подходящих "
            "вакансий за раз. Под каждой две кнопки:\n\n"
            "✍️ Написать сопроводительное — до 5 в день\n"
            "📝 Собрать резюме — до 3 в день\n\n"
            "Если захочешь скорректировать запрос — возвращайся в /edit_profile"
        )
    return (
        "Готово, ты в поиске 🚀\n\n"
        "Я буду присылать подборку раз в день — до 3 самых подходящих вакансий. "
        "Под каждой две кнопки:\n\n"
        "✍️ Написать сопроводительное — тебе доступно одно в день\n"
        "📝 Собрать резюме — доступно всего одно резюме\n\n"
        "Хочешь больше? На Pro я работаю несколько раз в день, присылаю до 5 вакансий "
        "за раз, пишу до 5 сопроводительных и собираю до 3 резюме в день. "
        "От 349₽ в неделю — /upgrade"
    )


async def try_auto_activate_search(bot: Bot, user_id: int) -> bool:
    """Проверяет готовность профиля юзера и активирует поиск если готов.

    Идемпотентная — вызывается после каждого обновления профиля.
    Если юзер уже активен ИЛИ профиль ещё не готов — ничего не делает.

    Возвращает True если активация только что произошла.
    """
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
        if user is None:
            return False

        # Уже активирован — тихо выходим
        if user.profile_ready_for_search:
            return False

        # Проверяем готовность профиля
        profile = (await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()

        ready, _reason = is_profile_ready(profile.profile_data if profile else None)
        if not ready:
            return False

        # Атомарно взводим флаг — защита от race condition при быстрых сообщениях
        user.profile_ready_for_search = True
        await session.commit()
        await session.refresh(user)

        logger.info(
            "auto_activated_search",
            extra={"user_id": user.id, "telegram_id": user.telegram_id},
        )

    # Дальше — асинхронно шлём сообщение и запускаем первый цикл
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=_build_activation_text(user),
        )
    except Exception as e:
        logger.warning(
            "auto_activation_message_failed",
            extra={"user_id": user.id, "error": str(e)},
        )

    # Триггерим первый матчинг в фоне
    from src.workers.job_search import _process_user
    asyncio.create_task(_process_user(bot, user))

    return True