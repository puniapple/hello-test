import asyncio
from sqlalchemy import select
from src.db.models import User, Source, SourceType
from src.db.session import async_session, engine

# Новые карьерные источники, которые добавляем активным юзерам
NEW_CAREER_SITES = [
    # Lever
    "eventbrite", "kayak", "quora", "brex", "ramp", "mixpanel",
    "faire", "loom", "census", "hex", "fivetran", "whatnot",
    "cresta", "persona", "netflix",
    # Ashby
    "notion", "linear", "posthog", "replicate", "cursor",
    "perplexity", "modal", "pinecone", "inngest", "resend",
    "trigger_dev", "supabase", "liveblocks", "railway", "elevenlabs",
]


async def main():
    async with async_session() as s:
        users = (await s.execute(
            select(User)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )).scalars().all()

        print(f"Активных юзеров: {len(users)}")
        print(f"Новых источников: {len(NEW_CAREER_SITES)}\n")

        added = 0
        for user in users:
            # Уже существующие source identifiers для этого юзера
            existing = (await s.execute(
                select(Source.identifier)
                .where(Source.user_id == user.id)
                .where(Source.source_type == SourceType.career_site)
            )).scalars().all()
            existing_set = set(existing)

            user_added = 0
            for site_id in NEW_CAREER_SITES:
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
                print(f"  {user.telegram_id} ({username}): +{user_added} источников")
                added += user_added

        await s.commit()
        print(f"\nИтого добавлено: {added} записей в sources")

    await engine.dispose()


asyncio.run(main())