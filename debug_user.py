"""Дебаг конкретного юзера: профиль, история сообщений, реакции."""
import asyncio
import sys
import json

from sqlalchemy import select
from src.db.models import ChatMessage, Profile, User, VacancyMatch
from src.db.session import async_session, engine


async def main(telegram_id: int):
    async with async_session() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == telegram_id)
        )).scalar_one_or_none()
        if user is None:
            print(f"Юзер {telegram_id} не найден")
            return

        print(f"=== USER ===")
        print(f"id: {user.id}, telegram_id: {user.telegram_id}")
        print(f"username: @{user.telegram_username}")
        print(f"state: {user.state}")
        print(f"is_active: {user.is_active}")

        profile = (await s.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()
        print(f"\n=== PROFILE ===")
        if profile is None:
            print("Профиль не создан")
        else:
            print(f"profile_data:")
            print(json.dumps(profile.profile_data or {}, ensure_ascii=False, indent=2))

        messages = (await s.execute(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at)
        )).scalars().all()
        print(f"\n=== CHAT MESSAGES ({len(messages)} total) ===")
        for m in messages:
            role = m.role.value if hasattr(m.role, "value") else m.role
            ctx = m.context.value if hasattr(m.context, "value") else m.context
            content_preview = (m.content or "")[:200]
            print(f"[{m.created_at}] {role} ({ctx}):")
            print(f"  {content_preview}")
            print()

        matches = (await s.execute(
            select(VacancyMatch).where(VacancyMatch.user_id == user.id)
        )).scalars().all()
        print(f"=== DELIVERED VACANCIES: {len(matches)} ===")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python debug_user.py <telegram_id>")
        sys.exit(1)
    asyncio.run(main(int(sys.argv[1])))