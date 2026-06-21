import asyncio
from sqlalchemy import select
from src.db.models import User, Source, SourceType
from src.db.session import async_session, engine

NEW_REMOTE_SOURCES = [
    "remoteok",
    "remotive",
    "wwr_programming",
    "wwr_sales_marketing",
    "wwr_customer_support",
    "wwr_product",
    "wwr_design",
]


async def main():
    async with async_session() as s:
        users = (await s.execute(
            select(User)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )).scalars().all()

        print(f"Активных юзеров: {len(users)}")
        print(f"Новых источников: {len(NEW_REMOTE_SOURCES)}\n")

        added = 0
        for user in users:
            existing = (await s.execute(
                select(Source.identifier)
                .where(Source.user_id == user.id)
                .where(Source.source_type == SourceType.career_site)
            )).scalars().all()
            existing_set = set(existing)

            user_added = 0
            for site_id in NEW_REMOTE_SOURCES:
                if site_id in existing_set:
                    continue
                s.add(Source(
                    user_id=user.id,
                    source_type=SourceType.career_site,
                    identifier=site_id,
                    is_active=True,
                ))
                user_added += 1

            if user_added > 0:
                username = f"@{user.telegram_username}" if user.telegram_username else "—"
                print(f"  {user.telegram_id} ({username}): +{user_added}")
                added += user_added

        await s.commit()
        print(f"\nИтого добавлено: {added}")

    await engine.dispose()


asyncio.run(main())