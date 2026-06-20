import asyncio
from sqlalchemy import select
from src.db.models import User, Profile
from src.db.session import async_session, engine

async def main():
    async with async_session() as s:
        result = await s.execute(
            select(User).where(User.telegram_id == 1328983336)
        )
        u = result.scalar_one_or_none()
        if not u:
            print("Юзер не найден")
            return

        print(f"User: id={u.id}, state={u.state}")
        print(f"is_active={u.is_active}, ready={u.profile_ready_for_search}")

        profile = (await s.execute(
            select(Profile).where(Profile.user_id == u.id)
        )).scalar_one_or_none()

        if profile:
            print(f"Profile exists, data: {profile.profile_data}")
        else:
            print("Profile NOT EXISTS in DB")

    await engine.dispose()

asyncio.run(main())