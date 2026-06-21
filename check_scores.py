import asyncio
from sqlalchemy import select
from src.db.models import VacancyMatch, User
from src.db.session import async_session, engine
from datetime import datetime, timedelta, timezone


async def main():
    async with async_session() as s:
        # Берём всех активных юзеров
        users = (await s.execute(
            select(User)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )).scalars().all()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        for user in users:
            matches = (await s.execute(
                select(VacancyMatch.match_score, VacancyMatch.sent_at, VacancyMatch.match_reason)
                .where(VacancyMatch.user_id == user.id)
                .where(VacancyMatch.sent_at >= cutoff)
                .order_by(VacancyMatch.match_score.desc())
            )).all()

            if not matches:
                continue

            username = f"@{user.telegram_username}" if user.telegram_username else "—"
            print(f"\n{user.telegram_id} ({username}): {len(matches)} записей за 24ч")

            # Гистограмма
            buckets = {">=4.5": 0, "4.0-4.5": 0, "3.5-4.0": 0, "3.0-3.5": 0, "<3.0": 0}
            for score, sent_at, reason in matches:
                if score is None:
                    continue
                if score >= 4.5: buckets[">=4.5"] += 1
                elif score >= 4.0: buckets["4.0-4.5"] += 1
                elif score >= 3.5: buckets["3.5-4.0"] += 1
                elif score >= 3.0: buckets["3.0-3.5"] += 1
                else: buckets["<3.0"] += 1

            for bucket, count in buckets.items():
                if count > 0:
                    print(f"    {bucket}: {count}")

            # Топ-5 с reason
            print(f"  Топ-5:")
            for score, sent_at, reason in matches[:5]:
                marker = "✓" if sent_at else " "
                reason_short = (reason or "")[:80]
                print(f"    {marker} {score} | {reason_short}")

    await engine.dispose()


asyncio.run(main())