# check_leak.py
import asyncio
from sqlalchemy import select
from src.db.models import Profile, User
from src.db.session import async_session, engine


async def main():
    async with async_session() as s:
        users = (await s.execute(select(User))).scalars().all()
        for u in users:
            p = (await s.execute(
                select(Profile).where(Profile.user_id == u.id)
            )).scalar_one_or_none()
            if not p or not p.profile_data:
                continue
            ideal = p.profile_data.get("ideal_work_description", "")
            if "Optimacros" in ideal:
                print(f"⚠️ @{u.telegram_username} (id: {u.telegram_id})")
                print(f"   ideal: {ideal[:300]}")
                print()
    await engine.dispose()


asyncio.run(main())