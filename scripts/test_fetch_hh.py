"""Smoke test: добавить тестовый hh-источник и забрать вакансии."""

import asyncio
import sys

from sqlalchemy import select

from src.db.models import SourceType, User
from src.db.session import async_session, engine
from src.services.sources_service import (
    add_source,
    filter_unseen,
    list_user_sources,
)
from src.sources.hh_ru import HHSource


async def main(query: str):
    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        if user is None:
            print("Нет ни одного юзера. Сначала /start в Telegram.")
            return

        # Создадим временный источник для теста
        source = await add_source(
            session=session,
            user_id=user.id,
            source_type=SourceType.hh_ru,
            identifier=query,
        )
        await session.commit()
        print(f"✓ Создан тестовый источник #{source.id}: «{query}»")

        # Fetch
        hh = HHSource()
        vacancies = await hh.fetch(source)
        print(f"✓ HH API вернул {len(vacancies)} вакансий")

        # Dedup
        new_only = await filter_unseen(session, user.id, vacancies)
        print(f"✓ Из них новых (не в seen_vacancies): {len(new_only)}")

        # Покажем первые 3
        for v in new_only[:3]:
            print(f"  — {v.title} @ {v.company or '?'} | {v.salary or 'з/п не указана'}")
            print(f"    {v.url}")

    await engine.dispose()


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "business development"
    asyncio.run(main(query))