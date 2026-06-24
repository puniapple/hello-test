import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, func
from src.db.session import async_session, engine
from src.db.models import ChatMessage, User


async def main():
    start = datetime(2026, 6, 23, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 24, 0, 0, 0, tzinfo=timezone.utc)

    async with async_session() as s:
        stmt = (
            select(
                ChatMessage.user_id,
                User.telegram_username,
                func.count(ChatMessage.id).label("msgs"),
                func.min(ChatMessage.created_at).label("first"),
                func.max(ChatMessage.created_at).label("last"),
            )
            .join(User, User.id == ChatMessage.user_id)
            .where(ChatMessage.created_at >= start)
            .where(ChatMessage.created_at < end)
            .group_by(ChatMessage.user_id, User.telegram_username)
            .order_by(func.count(ChatMessage.id).desc())
        )
        rows = (await s.execute(stmt)).all()

    print(f"\n📊 Активность с ботом 23 июня 2026 (UTC)\n")
    print(f"{'User':<25} {'Msgs':>5}  {'First UTC':<10} {'Last UTC':<10}")
    print("-" * 60)
    for r in rows:
        uname = f"@{r.telegram_username}" if r.telegram_username else f"id={r.user_id}"
        first = r.first.strftime("%H:%M:%S") if r.first else "-"
        last = r.last.strftime("%H:%M:%S") if r.last else "-"
        print(f"{uname:<25} {r.msgs:>5}  {first:<10} {last:<10}")

    print(f"\nВсего активных юзеров: {len(rows)}")
    print(f"Всего сообщений за день: {sum(r.msgs for r in rows)}")

    await engine.dispose()


asyncio.run(main())