"""Bot command handlers."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select

from src.db.models import Profile, User, UserState
from src.db.session import async_session

router = Router()


WELCOME_TEXT = (
    "Привет! 👋\n\n"
    "Я помогу тебе находить релевантные вакансии в Telegram-каналах и на hh.ru, "
    "матчить их под твой профиль и присылать только то, что реально подходит.\n\n"
    "Чтобы начать — давай соберём твой профиль через диалог.\n"
    "Нажми /edit_profile и расскажи о себе.\n\n"
    "Команды:\n"
    "/edit_profile — собрать или обновить профиль\n"
    "/show_profile — показать текущий профиль\n"
    "/pause — остановить присылку вакансий\n"
    "/resume — возобновить\n"
    "/help — это сообщение"
)


async def get_or_create_user(telegram_id: int, telegram_username: str | None) -> User:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, telegram_username=telegram_username)
            session.add(user)
            await session.flush()
            profile = Profile(user_id=user.id, profile_data={})
            session.add(profile)
            await session.commit()
            await session.refresh(user)
        return user


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await get_or_create_user(
        telegram_id=message.from_user.id,
        telegram_username=message.from_user.username,
    )
    await message.answer(WELCOME_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME_TEXT)


@router.message(Command("show_profile"))
async def cmd_show_profile(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Profile).join(User).where(User.telegram_id == message.from_user.id)
        )
        profile = result.scalar_one_or_none()
        if profile is None or not profile.profile_data:
            await message.answer(
                "Профиль пока пуст. Нажми /edit_profile, чтобы его собрать."
            )
            return
        await message.answer(f"Твой профиль:\n\n```\n{profile.profile_data}\n```", parse_mode="Markdown")


@router.message(Command("edit_profile"))
async def cmd_edit_profile(message: Message) -> None:
    await message.answer(
        "Скоро здесь будет полноценный диалог сборки профиля 🚧\n"
        "Пока заглушка. В следующей фазе подключим разговорного агента."
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if user:
            user.state = UserState.paused
            user.is_active = False
            await session.commit()
    await message.answer("Поставила на паузу. /resume — возобновить.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if user:
            user.state = UserState.idle
            user.is_active = True
            await session.commit()
    await message.answer("Возобновила. Жди вакансий ✨")