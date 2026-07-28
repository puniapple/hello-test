"""Auto-activate поиск после того как профиль стал ready-for-search.

Задача: после того как юзер завершил заполнение профиля (через агента, голос, PDF),
проверить готов ли профиль → если да и юзер ещё не активен, поставить is_active=True
и запустить первый матчинг.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select, update

from src.db.models import Profile, User
from src.db.session import async_session
from src.services.profile_validation import is_profile_ready

log = structlog.get_logger(__name__)


async def try_auto_activate_search(bot, user_id: int) -> None:
    """Если профиль стал ready и юзер не активен — активируем.
    
    Ничего не делаем если:
    - профиль не готов
    - юзер уже активен
    - юзер сам поставил на паузу (profile_ready_for_search=True но is_active=False)
    
    user_id — это внутренний User.id, не telegram_id.
    """
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
        if user is None:
            return
        
        profile = (await session.execute(
            select(Profile).where(Profile.user_id == user_id)
        )).scalar_one_or_none()
        if profile is None or not profile.profile_data:
            return
        
        ready, _reason = is_profile_ready(profile.profile_data)
        if not ready:
            return
        
        # Если уже активен и профиль ready — ничего не делаем
        if user.is_active and user.profile_ready_for_search:
            return
        
        # Если юзер на паузе (профиль был готов, но выключен вручную) — не активируем
        if user.profile_ready_for_search and not user.is_active:
            log.info("skip_activation_user_on_pause", user_id=user_id)
            return
        
        # Активируем
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(profile_ready_for_search=True)
        )
        await session.commit()
        log.info("profile_auto_activated", user_id=user_id)