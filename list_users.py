import asyncio
import json
from sqlalchemy import select
from src.db.models import Profile, User
from src.db.session import async_session, engine


async def main():
    async with async_session() as s:
        users = (await s.execute(select(User))).scalars().all()
        for u in users:
            profile = (await s.execute(
                select(Profile).where(Profile.user_id == u.id)
            )).scalar_one_or_none()
            pd = profile.profile_data if profile else None
            pd_status = "пусто" if not pd else f"keys: {list(pd.keys())[:5]}"
            print(f"db_id={u.id}  tg_id={u.telegram_id}  @{u.telegram_username}  state={u.state}  profile={pd_status}")
    await engine.dispose()


asyncio.run(main())