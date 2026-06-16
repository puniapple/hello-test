import asyncio
import json
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
            pd_str = json.dumps(p.profile_data, ensure_ascii=False)
            if "Optimacros" in pd_str:
                print(f"⚠️ @{u.telegram_username} (id: {u.telegram_id})")
                for key, value in p.profile_data.items():
                    if "Optimacros" in str(value):
                        print(f"   {key}: {str(value)[:200]}")
                print()
    await engine.dispose()


asyncio.run(main())