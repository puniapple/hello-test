"""Прогон всех источников для конкретного юзера, с показом count'а по каждому.
Использование: python check_sources.py <telegram_id>
"""
import asyncio
import sys

from sqlalchemy import select
from src.db.models import SourceType, User
from src.db.session import async_session, engine
from src.services.sources_service import list_user_sources
from src.sources.career_sites import CareerSiteSource, get_company_name
from src.sources.telegram_channel import TelegramChannelSource


async def main(telegram_id: int):
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )).scalar_one_or_none()
        if user is None:
            print(f"Юзер {telegram_id} не найден")
            return

        username = f"@{user.telegram_username}" if user.telegram_username else "—"
        sources = await list_user_sources(session, user.id)
        print(f"\n{username} (id: {telegram_id})")
        print(f"Активных источников: {len(sources)}\n")

        tg = TelegramChannelSource()
        cs = CareerSiteSource()

        results: list[tuple[str, str, int]] = []

        for s in sources:
            try:
                if s.source_type == SourceType.telegram_channel:
                    vacancies = await asyncio.wait_for(tg.fetch(s), timeout=30)
                    label = f"@{s.identifier}"
                    kind = "TG"
                elif s.source_type == SourceType.career_site:
                    vacancies = await asyncio.wait_for(cs.fetch(s), timeout=30)
                    label = get_company_name(s.identifier) or s.identifier
                    kind = "WEB"
                else:
                    continue
                count = len(vacancies)
                results.append((kind, label, count))
                print(f"  {kind:4} {label:30} → {count}")
            except asyncio.TimeoutError:
                print(f"  ⏱  {s.identifier:30} → timeout")
            except Exception as e:
                print(f"  ✗  {s.identifier:30} → {e}")

        print(f"\n{'═' * 50}")
        total = sum(c for _, _, c in results)
        print(f"Всего вакансий: {total}")
        print(f"\nТОП-5 источников:")
        for kind, label, count in sorted(results, key=lambda x: x[2], reverse=True)[:5]:
            print(f"  {count:4} {kind:4} {label}")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python check_sources.py <telegram_id>")
        sys.exit(1)
    asyncio.run(main(int(sys.argv[1])))