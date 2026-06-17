"""Очистка истории seen-вакансий и доставленных матчей для одного юзера.
Использование: python clear_seen.py <telegram_id>
"""
import asyncio
import sys

from sqlalchemy import delete, select
from src.db.models import SeenVacancy, User, VacancyMatch
from src.db.session import async_session, engine


async def main(telegram_id: int):
    async with async_session() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == telegram_id)
        )).scalar_one_or_none()
        if user is None:
            print(f"Юзер {telegram_id} не найден")
            return

        seen_result = await s.execute(
            delete(SeenVacancy).where(SeenVacancy.user_id == user.id)
        )
        matches_result = await s.execute(
            delete(VacancyMatch).where(VacancyMatch.user_id == user.id)
        )
        await s.commit()

        print(f"Очищено для {telegram_id}:")
        print(f"  seen_vacancies: {seen_result.rowcount}")
        print(f"  vacancy_matches: {matches_result.rowcount}")
    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python clear_seen.py <telegram_id>")
        sys.exit(1)
    asyncio.run(main(int(sys.argv[1])))