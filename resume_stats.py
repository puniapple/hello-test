"""Статистика по сгенерированным резюме.

Запуск: python3 resume_stats.py

Выводит:
  1. Сводка: сколько всего, сколько уникальных юзеров
  2. Дневная динамика за 7 календарных дней (UTC)
  3. Топ юзеров по количеству резюме
  4. Юзеры, которые попробовали фичу, но не жали повторно
     (потенциальные "не понравилось" — сигнал качества)
  5. Разбивка по тарифам (free/pro/grandfather)
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from src.db.models import Profile, ResumeUsage, User, VacancyMatch
from src.db.session import async_session, engine


async def main():
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    async with async_session() as s:
        # ─── 1. Сводка ───
        total_resumes = (await s.execute(
            select(func.count(ResumeUsage.id))
        )).scalar() or 0

        unique_users = (await s.execute(
            select(func.count(func.distinct(ResumeUsage.user_id)))
        )).scalar() or 0

        print("📄 РЕЗЮМЕ — СВОДКА")
        print(f"  Всего сгенерировано: {total_resumes}")
        print(f"  Уникальных юзеров:   {unique_users}")
        if unique_users:
            print(f"  Среднее на юзера:    {total_resumes / unique_users:.1f}")
        print()

        if total_resumes == 0:
            print("Пока ни одного резюме не сгенерировано.")
            await engine.dispose()
            return

        # ─── 2. Дневная динамика (7 дней UTC) ───
        print("📊 Динамика по календарным дням (UTC):")
        print(f"  {'Дата':<12} {'Резюме':>8} {'Юзеров':>8}")
        print(f"  {'-' * 12} {'-' * 8} {'-' * 8}")

        # Сегодня
        today_count = (await s.execute(
            select(func.count(ResumeUsage.id))
            .where(ResumeUsage.generated_at >= today_utc)
        )).scalar() or 0
        today_users = (await s.execute(
            select(func.count(func.distinct(ResumeUsage.user_id)))
            .where(ResumeUsage.generated_at >= today_utc)
        )).scalar() or 0
        print(f"  {today_utc.strftime('%Y-%m-%d')} {today_count:>8} {today_users:>8}  (сегодня)")

        # 7 полных дней назад
        for days_ago in range(1, 8):
            day_start = today_utc - timedelta(days=days_ago)
            day_end = today_utc - timedelta(days=days_ago - 1)

            count = (await s.execute(
                select(func.count(ResumeUsage.id))
                .where(ResumeUsage.generated_at >= day_start)
                .where(ResumeUsage.generated_at < day_end)
            )).scalar() or 0
            users = (await s.execute(
                select(func.count(func.distinct(ResumeUsage.user_id)))
                .where(ResumeUsage.generated_at >= day_start)
                .where(ResumeUsage.generated_at < day_end)
            )).scalar() or 0
            print(f"  {day_start.strftime('%Y-%m-%d')} {count:>8} {users:>8}")
        print()

        # ─── 3. Топ юзеров ───
        print("🏆 Топ юзеров по количеству резюме:")
        top_users = (await s.execute(
            select(
                ResumeUsage.user_id,
                func.count(ResumeUsage.id).label("cnt"),
                func.max(ResumeUsage.generated_at).label("last"),
            )
            .group_by(ResumeUsage.user_id)
            .order_by(func.count(ResumeUsage.id).desc())
            .limit(15)
        )).all()

        for user_id, cnt, last in top_users:
            user = (await s.execute(
                select(User).where(User.id == user_id)
            )).scalar_one_or_none()
            if not user:
                continue
            username = f"@{user.telegram_username}" if user.telegram_username else "—"
            plan_marker = {
                "free": "F",
                "pro": "💎P",
                "grandfather": "👑G",
            }.get((user.plan or "free").lower(), "?")
            last_str = last.strftime("%d.%m %H:%M") if last else "?"
            print(f"  {username:<25} [{plan_marker}] {cnt:>3} резюме, последнее: {last_str}")
        print()

        # ─── 4. Разбивка по тарифам ───
        print("💳 Разбивка по тарифам:")
        by_plan = (await s.execute(
            select(
                User.plan,
                func.count(func.distinct(ResumeUsage.user_id)).label("users"),
                func.count(ResumeUsage.id).label("resumes"),
            )
            .join(ResumeUsage, ResumeUsage.user_id == User.id)
            .group_by(User.plan)
        )).all()

        for plan, users, resumes in by_plan:
            plan_label = (plan or "free").ljust(12)
            avg = resumes / users if users else 0
            print(f"  {plan_label} юзеров: {users:>3}, резюме: {resumes:>4}, среднее: {avg:.1f}")
        print()

        # ─── 5. "Попробовали и не вернулись" ───
        # Free-юзеры, у которых доступно ровно 1 резюме — из них можно только
        # смотреть, кто использовал (лимит достигнут). Интереснее Pro-юзеры,
        # которые сгенерили 1 резюме и больше не возвращались 3+ дня.
        cutoff_3d = now_utc - timedelta(days=3)
        stale_paid = (await s.execute(
            select(
                ResumeUsage.user_id,
                func.count(ResumeUsage.id).label("cnt"),
                func.max(ResumeUsage.generated_at).label("last"),
            )
            .join(User, User.id == ResumeUsage.user_id)
            .where(User.plan.in_(["pro", "grandfather"]))
            .group_by(ResumeUsage.user_id)
            .having(func.count(ResumeUsage.id) == 1)
            .having(func.max(ResumeUsage.generated_at) < cutoff_3d)
        )).all()

        if stale_paid:
            print("🤔 Pro/Grandfather — попробовали 1 раз и не вернулись >3 дней:")
            for user_id, cnt, last in stale_paid:
                user = (await s.execute(
                    select(User).where(User.id == user_id)
                )).scalar_one_or_none()
                if not user:
                    continue
                username = f"@{user.telegram_username}" if user.telegram_username else "—"
                days_ago = (now_utc - last).days if last else "?"
                print(f"  {username:<25} — {days_ago} дн. назад")
            print()

        # ─── 6. Стоимость (оценка) ───
        # Sonnet ~ $0.03 на среднее резюме (input ~4K tokens, output ~1.5K)
        est_cost = total_resumes * 0.03
        print(f"💰 Оценка стоимости Sonnet за всё время: ~${est_cost:.2f}")
        print(f"   (~$0.03 за резюме, реальная цифра — в Anthropic Console)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())