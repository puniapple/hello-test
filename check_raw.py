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
        if user is None:
            print("User not found")
            return

        profile = (await s.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()

        print(f"user.id = {user.id}")
        print(f"profile object exists: {profile is not None}")
        if profile is not None:
            print(f"profile_data type: {type(profile.profile_data)}")
            print(f"profile_data value: {repr(profile.profile_data)}")
            print(f"bool(profile_data): {bool(profile.profile_data)}")
            if profile.profile_data:
                print(f"keys: {list(profile.profile_data.keys())}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))