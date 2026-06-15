"""Smoke test: fetch all career sites for first user."""

import asyncio
import sys

from sqlalchemy import select

from src.db.models import SourceType, User
from src.db.session import async_session, engine
from src.services.source_provisioning import provision_default_sources
from src.services.sources_service import filter_unseen, list_user_sources
from src.sources.career_sites import CareerSiteSource, get_company_name


async def main(site_filter: str | None = None):
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

        sources = await list_user_sources(session, user.id, SourceType.career_site)
        if site_filter:
            sources = [s for s in sources if site_filter in s.identifier]
        print(f"✓ Активных career-источников: {len(sources)}")

        cs = CareerSiteSource()
        total_new = 0

        for source in sources:
            company = get_company_name(source.identifier) or source.identifier
            try:
                vacancies = await cs.fetch(source)
            except Exception as e:
                print(f"  ✗ {company}: ошибка — {e}")
                continue

            new_only = await filter_unseen(session, user.id, vacancies)
            total_new += len(new_only)
            print(f"  [{company}] всего {len(vacancies)}, новых {len(new_only)}")

            if new_only:
                v = new_only[0]
                title = v.title[:80] + ("..." if len(v.title) > 80 else "")
                print(f"    → {title}")
                if v.salary:
                    print(f"      💰 {v.salary}")
                if v.location:
                    print(f"      📍 {v.location}")
                print(f"      🔗 {v.url}")

        print(f"\nИтого новых: {total_new}")

    await engine.dispose()


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(site))