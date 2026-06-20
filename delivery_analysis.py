import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from src.db.models import User, VacancyMatch, SeenVacancy, Profile
from src.db.session import async_session, engine


async def main():
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)

    async with async_session() as s:
        # Все активные юзеры
        result = await s.execute(
            select(User)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )
        users = result.scalars().all()

        print(f"Анализ {len(users)} активных юзеров:\n")

        for u in users:
            # Доставлено за 24ч
            delivered_24h = (await s.execute(
                select(func.count(VacancyMatch.id))
                .where(VacancyMatch.user_id == u.id)
                .where(VacancyMatch.sent_at >= cutoff_24h)
            )).scalar()

            # Доставлено за 7д
            delivered_7d = (await s.execute(
                select(func.count(VacancyMatch.id))
                .where(VacancyMatch.user_id == u.id)
                .where(VacancyMatch.sent_at >= cutoff_7d)
            )).scalar()

            # Всего seen (показатель "истощённости пула")
            seen_total = (await s.execute(
                select(func.count(SeenVacancy.id))
                .where(SeenVacancy.user_id == u.id)
            )).scalar()

            # Есть ли профиль
            profile = (await s.execute(
                select(Profile).where(Profile.user_id == u.id)
            )).scalar_one_or_none()
            has_profile = "✅" if profile and profile.profile_data else "❌"

            username = f"@{u.telegram_username}" if u.telegram_username else "—"
            print(f"  {u.telegram_id} ({username})")
            print(f"    Профиль: {has_profile}, Seen всего: {seen_total}")
            print(f"    Доставлено: 24ч={delivered_24h}, 7д={delivered_7d}")
            print()

    await engine.dispose()


asyncio.run(main())