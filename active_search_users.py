import asyncio
from sqlalchemy import select
from src.db.models import User
from src.db.session import async_session, engine


async def main():
    async with async_session() as s:
        result = await s.execute(
            select(User)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
            .order_by(User.created_at.desc())
        )
        users = result.scalars().all()

        print(f"Активных юзеров с включённым поиском: {len(users)}\n")

        for u in users:
            username = f"@{u.telegram_username}" if u.telegram_username else "—"
            created = u.created_at.strftime("%Y-%m-%d") if u.created_at else "?"
            print(f"  {u.telegram_id} ({username}) — с {created}")

    await engine.dispose()


asyncio.run(main())