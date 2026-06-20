import asyncio
import json
from sqlalchemy import select
from src.db.models import User, Profile
from src.db.session import async_session, engine


async def main():
    async with async_session() as s:
        result = await s.execute(
            select(User, Profile)
            .join(Profile, Profile.user_id == User.id)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )
        rows = result.all()

        output = []
        for user, profile in rows:
            if not profile.profile_data:
                continue
            output.append({
                "telegram_id": user.telegram_id,
                "username": user.telegram_username,
                "profile_data": profile.profile_data,
            })

        with open("profiles_backup.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"Сохранено профилей: {len(output)}")
        print(f"Файл: profiles_backup.json")

    await engine.dispose()


asyncio.run(main())