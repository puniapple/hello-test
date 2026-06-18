
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from src.db.models import User, VacancyMatch, ChatMessage
from src.db.session import async_session, engine


async def main():
    async with async_session() as s:
        # Юзеры
        total_users = (await s.execute(select(func.count(User.id)))).scalar()
        active = (await s.execute(
            select(func.count(User.id))
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )).scalar()

        # За последние 3 дня
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)

        matches_count = (await s.execute(
            select(func.count(VacancyMatch.id))
            .where(VacancyMatch.sent_at >= cutoff)
        )).scalar()

        messages_count = (await s.execute(
            select(func.count(ChatMessage.id))
            .where(ChatMessage.created_at >= cutoff)
        )).scalar()

        print(f"Юзеров всего: {total_users}")
        print(f"Активных: {active}")
        print(f"\nЗа последние 3 дня:")
        print(f"  Доставлено вакансий (создано VacancyMatch): {matches_count}")
        print(f"  Сообщений в чатах (юзер↔Claude): {messages_count}")
        print(f"\nГрубая оценка реального расхода:")
        # Haiku: 1 матчинг = $0.005, но через matcher проходит больше чем доставлено (порог отсекает)
        # ~30 матчингов даёт ~5 доставок, т.е. реальных матчингов ~6x от доставленных
        haiku_cost = matches_count * 6 * 0.005
        # Sonnet: ~$0.05 за сообщение (input+output)
        sonnet_cost = messages_count * 0.05
        print(f"  Haiku (~{matches_count * 6} матчингов × $0.005): ${haiku_cost:.2f}")
        print(f"  Sonnet ({messages_count} сообщений × ~$0.05): ${sonnet_cost:.2f}")
        print(f"  Итого: ${haiku_cost + sonnet_cost:.2f}")

    await engine.dispose()


asyncio.run(main())
