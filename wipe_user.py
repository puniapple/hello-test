"""One-off: полностью обнуляет юзера, как будто он новый.

Чистит: Profile, ChatMessage, VacancyMatch, Source.
Сбрасывает: state, profile_ready_for_search, is_active.
НЕ удаляет: сам User (telegram_id, username сохраняются).

После запуска — попроси юзера нажать /start, он пройдёт онбординг с нуля.
"""
import asyncio
import sys
from sqlalchemy import delete, select, update
from src.db.session import async_session, engine
from src.db.models import (
    ChatMessage,
    Profile,
    Source,
    User,
    UserState,
    VacancyMatch,
)

TARGET_TELEGRAM_ID = 119210152


async def main():
    async with async_session() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == TARGET_TELEGRAM_ID)
        )).scalar_one_or_none()

        if not user:
            print(f"User with telegram_id={TARGET_TELEGRAM_ID} not found")
            return

        username = f"@{user.telegram_username}" if user.telegram_username else "—"
        print(f"Wiping user {user.id} ({username}, tg={user.telegram_id})")

        profile_deleted = (await s.execute(
            delete(Profile).where(Profile.user_id == user.id)
        )).rowcount
        chat_deleted = (await s.execute(
            delete(ChatMessage).where(ChatMessage.user_id == user.id)
        )).rowcount
        matches_deleted = (await s.execute(
            delete(VacancyMatch).where(VacancyMatch.user_id == user.id)
        )).rowcount
        sources_deleted = (await s.execute(
            delete(Source).where(Source.user_id == user.id)
        )).rowcount

        await s.execute(
            update(User)
            .where(User.id == user.id)
            .values(
                state=UserState.idle,
                profile_ready_for_search=False,
                is_active=False,
            )
        )
        await s.commit()

        print(f"  Profile rows deleted: {profile_deleted}")
        print(f"  ChatMessage rows deleted: {chat_deleted}")
        print(f"  VacancyMatch rows deleted: {matches_deleted}")
        print(f"  Source rows deleted: {sources_deleted}")
        print(f"  User reset: state=idle, profile_ready_for_search=False, is_active=False")
        print("\nDone. Tell user to send /start to begin fresh onboarding.")

    await engine.dispose()


if __name__ == "__main__":
    if "--yes" not in sys.argv:
        print(f"This will WIPE all data for telegram_id={TARGET_TELEGRAM_ID}.")
        print("Re-run with --yes to confirm:")
        print(f"  python {sys.argv[0]} --yes")
        sys.exit(1)
    asyncio.run(main())