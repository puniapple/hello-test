import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, distinct
from src.db.models import User, SeenVacancy
from src.db.session import async_session, engine


async def main():
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    async with async_session() as s:
        # ─── Регистрации и активность по календарным дням ───
        print("📊 Динамика по календарным дням (UTC):\n")
        print(f"  {'Дата':<12} {'Новых':>7} {'В матчинге':>12}")
        print(f"  {'-' * 12} {'-' * 7} {'-' * 12}")

        # Сегодня
        new_today = (await s.execute(
            select(func.count(User.id))
            .where(User.created_at >= today_start)
        )).scalar()

        active_today = (await s.execute(
            select(func.count(distinct(SeenVacancy.user_id)))
            .where(SeenVacancy.sent_at >= today_start)
        )).scalar()

        print(f"  {today_start.strftime('%Y-%m-%d')} {new_today:>7} {active_today:>12}  (сегодня)")

        # Последние 7 полных календарных дней
        for days_ago in range(1, 8):
            day_start = today_start - timedelta(days=days_ago)
            day_end = today_start - timedelta(days=days_ago - 1)

            new_count = (await s.execute(
                select(func.count(User.id))
                .where(User.created_at >= day_start)
                .where(User.created_at < day_end)
            )).scalar()

            active_count = (await s.execute(
                select(func.count(distinct(SeenVacancy.user_id)))
                .where(SeenVacancy.sent_at >= day_start)
                .where(SeenVacancy.sent_at < day_end)
            )).scalar()

            print(f"  {day_start.strftime('%Y-%m-%d')} {new_count:>7} {active_count:>12}")

        print()

        # ─── Текущий список активных юзеров ───
        result = await s.execute(
            select(User)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
            .order_by(User.created_at.desc())
        )
        users = result.scalars().all()

        print(f"Сейчас активных юзеров с включённым поиском: {len(users)}\n")

        for u in users:
            username = f"@{u.telegram_username}" if u.telegram_username else "—"
            created = u.created_at.strftime("%Y-%m-%d") if u.created_at else "?"
            print(f"  {u.telegram_id} ({username}) — с {created}")

    await engine.dispose()


asyncio.run(main())