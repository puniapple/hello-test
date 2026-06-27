import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from src.db.models import User, VacancyMatch, SeenVacancy, Profile
from src.db.session import async_session, engine


async def main():
    now_utc = datetime.now(timezone.utc)
    cutoff_24h = now_utc - timedelta(hours=24)
    cutoff_7d = now_utc - timedelta(days=7)

    # Границы календарных дней (UTC) за последнюю неделю
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    async with async_session() as s:
        # ─── Сводка по календарным дням ───
        print("📊 Доставлено по календарным дням (UTC):\n")

        # Сегодня — неполный день, отдельно
        delivered_today = (await s.execute(
            select(func.count(VacancyMatch.id))
            .where(VacancyMatch.sent_at >= today_start)
        )).scalar()
        print(f"  {today_start.strftime('%Y-%m-%d')} (сегодня, с 00:00 UTC): {delivered_today}")

        # Последние 7 полных календарных дней
        for days_ago in range(1, 8):
            day_start = today_start - timedelta(days=days_ago)
            day_end = today_start - timedelta(days=days_ago - 1)

            delivered = (await s.execute(
                select(func.count(VacancyMatch.id))
                .where(VacancyMatch.sent_at >= day_start)
                .where(VacancyMatch.sent_at < day_end)
            )).scalar()
            print(f"  {day_start.strftime('%Y-%m-%d')}: {delivered}")

        print()

        # ─── Все активные юзеры ───
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