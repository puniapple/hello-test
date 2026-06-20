import asyncio
from sqlalchemy import select
from src.db.models import User, UserState
from src.db.session import async_session, engine


async def main():
    async with async_session() as s:
        result = await s.execute(
            select(User).where(User.telegram_id == 1328983336)
        )
        u = result.scalar_one_or_none()
        if not u:
            print("Юзер не найден")
            return

        # Сбрасываем флаги: воркер перестанет тратить ресурсы
        u.state = UserState.idle
        u.profile_ready_for_search = False
        await s.commit()
        print(f"Готово. state={u.state}, ready={u.profile_ready_for_search}")

    await engine.dispose()


asyncio.run(main())