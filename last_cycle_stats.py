import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from src.db.models import User, VacancyMatch
from src.db.session import async_session, engine


async def main():
    # Берём матчи за последние 4 часа (с запасом, чтобы точно поймать последний цикл)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    async with async_session() as s:
        # По юзерам: сколько каждому доставлено
        result = await s.execute(
            select(
                User.telegram_id,
                User.telegram_username,
                func.count(VacancyMatch.id).label("delivered"),
            )
            .join(VacancyMatch, VacancyMatch.user_id == User.id)
            .where(VacancyMatch.sent_at >= cutoff)
            .group_by(User.id, User.telegram_id, User.telegram_username)
            .order_by(func.count(VacancyMatch.id).desc())
        )
        rows = result.all()

        total = sum(r.delivered for r in rows)

        print(f"За последние 24 часа доставлено всего: {total} вакансий")
        print(f"Юзеров получили вакансии: {len(rows)}\n")

        if rows:
            print("По юзерам:")
            for r in rows:
                username = f"@{r.telegram_username}" if r.telegram_username else "—"
                print(f"  {r.telegram_id} ({username}): {r.delivered}")
        else:
            print("Никому ничего не доставлено.")

    await engine.dispose()


asyncio.run(main())
