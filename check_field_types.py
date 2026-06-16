import asyncio
import json
import sys
from sqlalchemy import select
from src.db.models import Profile, User
from src.db.session import async_session, engine


async def main(telegram_id: int):
    async with async_session() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == telegram_id)
        )).scalar_one_or_none()
        profile = (await s.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()

        pd = profile.profile_data
        for key, value in pd.items():
            print(f"{key}: type={type(value).__name__}, value={repr(value)[:200]}")
    await engine.dispose()


asyncio.run(main(int(sys.argv[1])))