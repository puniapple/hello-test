import asyncio
from sqlalchemy import select
from src.db.models import User, Source, SourceType
from src.db.session import async_session, engine


async def main():
    async with async_session() as s:
        users = (await s.execute(
            select(User)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )).scalars().all()

        print(f"Активных юзеров: {len(users)}")

        added = 0
        for user in users:
            existing = (await s.execute(
                select(Source.identifier)
                .where(Source.user_id == user.id)
                .where(Source.source_type == SourceType.career_site)
            )).scalars().all()
            if "habr_career" in existing:
                continue

            s.add(Source(
                user_id=user.id,
                source_type=SourceType.career_site,
                identifier="habr_career",
                is_active=True,
            ))
            username = f"@{user.telegram_username}" if user.telegram_username else "—"
            print(f"  {user.telegram_id} ({username}): +habr_career")
            added += 1

        await s.commit()
        print(f"\nИтого добавлено: {added}")

    await engine.dispose()


asyncio.run(main())