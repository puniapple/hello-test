"""Smoke test: parse all configured TG channels for the first user."""

import asyncio
import sys

from sqlalchemy import select

from src.db.models import SourceType, User
from src.db.session import async_session, engine
from src.services.source_provisioning import provision_default_sources
from src.services.sources_service import filter_unseen, list_user_sources
from src.sources.telegram_channel import TelegramChannelSource


async def main(channel_filter: str | None = None):
    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        if user is None:
            print("Нет ни одного юзера. Сначала /start в Telegram.")
            return

        created = await provision_default_sources(session, user.id)
        await session.commit()
        if created:
            print(f"✓ Создано новых источников: {created}")

        sources = await list_user_sources(session, user.id, SourceType.telegram_channel)
        if channel_filter:
            sources = [s for s in sources if channel_filter in s.identifier]

        print(f"✓ Активных TG-источников: {len(sources)}")

        tg = TelegramChannelSource()
        total_new = 0

        for source in sources:
            try:
                vacancies = await tg.fetch(source)
            except Exception as e:
                print(f"  ✗ {source.identifier}: ошибка — {e}")
                continue

            new_only = await filter_unseen(session, user.id, vacancies)
            total_new += len(new_only)
            print(
                f"  @{source.identifier}: всего {len(vacancies)} постов, "
                f"новых вакансий {len(new_only)}"
            )

            # Покажем первую новую для контроля
            if new_only:
                v = new_only[0]
                preview = v.title[:80] + ("..." if len(v.title) > 80 else "")
                print(f"    → {preview}")
                if v.salary:
                    print(f"      💰 {v.salary}")
                print(f"      🔗 {v.url}")

        print(f"\nИтого новых вакансий: {total_new}")

    await engine.dispose()


if __name__ == "__main__":
    filter_arg = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(filter_arg))