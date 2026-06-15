"""End-to-end smoke test: fetch from all sources, match, show top results."""

import asyncio

from sqlalchemy import select

from src.agents.matcher import VacancyMatcher
from src.db.models import Profile, SourceType, User
from src.db.session import async_session, engine
from src.services.sources_service import filter_unseen, list_user_sources, mark_seen
from src.sources.career_sites import CareerSiteSource, get_company_name
from src.sources.telegram_channel import TelegramChannelSource


async def main():
    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        if user is None:
            print("Нет ни одного юзера.")
            return

        profile_result = await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )
        profile = profile_result.scalar_one_or_none()
        if profile is None or not profile.profile_data:
            print("У юзера пустой профиль. Сначала /edit_profile.")
            return

        # Собираем все источники
        sources = await list_user_sources(session, user.id)
        print(f"Активных источников: {len(sources)}")

        tg = TelegramChannelSource()
        cs = CareerSiteSource()

        all_new: list = []
        for source in sources:
            try:
                if source.source_type == SourceType.telegram_channel:
                    fetched = await tg.fetch(source)
                elif source.source_type == SourceType.career_site:
                    fetched = await cs.fetch(source)
                else:
                    continue
            except Exception as e:
                print(f"  ✗ {source.identifier}: {e}")
                continue

            new_only = await filter_unseen(session, user.id, fetched)
            all_new.extend(new_only)

        print(f"Всего новых уникальных вакансий (после дедупа): {len(all_new)}")
        if not all_new:
            return

        matcher = VacancyMatcher()
        scored: list[tuple] = []

        # Ограничим прогон первой пачкой, чтобы не сжечь токены
        sample = all_new
        print(f"Прогоняю через matcher первые {len(sample)} вакансий...")
        
        # Защита от случайного перерасхода токенов
        estimated_cost_usd = len(sample) * 0.005
        print(f"Примерная стоимость прогона: ${estimated_cost_usd:.2f}")
        if estimated_cost_usd > 5.0:
            response = input(f"Уверена? Будет потрачено ~${estimated_cost_usd:.2f}. Продолжить? (y/n): ")
            if response.lower() != "y":
                print("Отменено")
                return


        for i, vacancy in enumerate(sample, 1):
            result = await matcher.match(profile.profile_data, vacancy)
            scored.append((result.score, result, vacancy))
            mark = "✓" if result.should_send else "·"
            print(f"  {mark} [{result.score:.1f}] {vacancy.title[:70]}")

        # Не помечаем как seen — это тест, можем перезапустить

        # Распределение по диапазонам — увидеть форму "пирамиды"
        buckets = {"9-10": 0, "8-9": 0, "7-8": 0, "6-7": 0, "5-6": 0, "<5": 0}
        for score, _, _ in scored:
            if score >= 9: buckets["9-10"] += 1
            elif score >= 8: buckets["8-9"] += 1
            elif score >= 7: buckets["7-8"] += 1
            elif score >= 6: buckets["6-7"] += 1
            elif score >= 5: buckets["5-6"] += 1
            else: buckets["<5"] += 1
        print("\nРаспределение скоров:")
        for bucket, count in buckets.items():
            bar = "█" * count
            print(f"  {bucket}: {count:3d} {bar}")

        scored.sort(key=lambda x: x[0], reverse=True)
        print("\n=== ТОП-10 ПО РЕЛЕВАНТНОСТИ ===\n")
        for score, result, vacancy in scored[:10]:
            company = get_company_name(vacancy.raw.get("site", "")) or vacancy.company or "?"
            print(f"[{score:.1f}] {vacancy.title}")
            print(f"  Источник: {company} ({vacancy.source_type.value})")
            print(f"  Почему: {result.fit_reason}")
            if result.red_flags:
                print(f"  Red flags: {', '.join(result.red_flags)}")
            print(f"  {vacancy.url}\n")

    await engine.dispose()


asyncio.run(main())