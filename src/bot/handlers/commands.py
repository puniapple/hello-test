"""Bot command handlers."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from src.agents.profile_agent import ProfileAgent
from src.services.claude import ClaudeService
from src.services.source_provisioning import provision_default_sources

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
    "/done — завершить редактирование профиля"
)


async def get_or_create_user(telegram_id: int, telegram_username: str | None) -> User:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        is_new = False
        if user is None:
            user = User(telegram_id=telegram_id, telegram_username=telegram_username)
            session.add(user)
            await session.flush()
            profile = Profile(user_id=user.id, profile_data={})
            session.add(profile)
            is_new = True

        # Provision default sources (idempotent — safe to call for existing users)
        await provision_default_sources(session, user.id)

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
            await message.answer("Профиль пока пуст. Нажми /edit_profile, чтобы его собрать.")
            return
        await message.answer(format_profile(profile.profile_data), parse_mode="MarkdownV2")


@router.message(Command("edit_profile"))
async def cmd_edit_profile(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Сначала напиши /start.")
            return

    claude = ClaudeService()
    agent = ProfileAgent(claude=claude)

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    kickoff_text, first_reply = await agent.start_editing(user_id=user.id)
    await message.answer(kickoff_text)
    await message.answer(first_reply.text)

@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Сначала напиши /start.")
            return
        if user.state == UserState.editing_profile:
            user.state = UserState.idle
            await session.commit()
            await message.answer(
                "Окей, вышла из режима редактирования. /show_profile — посмотреть, "
                "что собрали. /edit_profile — продолжить позже."
            )
        else:
            await message.answer("Ты сейчас не в режиме редактирования — нечего завершать.")

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


def _escape_md(text: str) -> str:
    """Escape MarkdownV2 special characters in user content."""
    chars = r"_*[]()~`>#+-=|{}.!\\"
    result = []
    for ch in str(text):
        if ch in chars:
            result.append("\\" + ch)
        else:
            result.append(ch)
    return "".join(result)


def format_profile(data: dict) -> str:
    """Render profile_data as a human-readable Russian message (MarkdownV2)."""
    if not data:
        return "Профиль пуст\\."

    labels = {
        "ideal_work_description": "🎯 Идеальная работа",
        "expertise": "💼 Экспертиза",
        "current_role_summary": "📍 Сейчас",
        "interests_and_resonance": "✨ К чему лежит душа",
        "target_roles": "🎯 Целевые роли",
        "anti_roles": "🚫 Не хочу",
        "industries_interested": "🏭 Интересные индустрии",
        "industries_avoid": "❌ Индустрии — нет",
        "location_preferences": "📍 Локация",
        "format": "🕐 Формат",
        "compensation": "💰 Компенсация",
        "languages": "🗣 Языки",
        "seniority": "📊 Уровень",
        "must_haves": "✅ Обязательно должно быть",
        "deal_breakers": "⛔ Точно нет",
        "free_form_notes": "📝 Заметки",
    }

    parts = ["*Твой профиль*", ""]
    for key, label in labels.items():
        value = data.get(key)
        if not value:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(v) for v in value)
        elif isinstance(value, dict):
            rendered = ", ".join(f"{k}: {v}" for k, v in value.items() if v)
        else:
            rendered = str(value)
        parts.append(f"*{_escape_md(label)}*")
        parts.append(_escape_md(rendered))
        parts.append("")

    cv_sources = data.get("cv_sources") or []
    if cv_sources:
        parts.append(f"*📄 Загруженные резюме \\({len(cv_sources)}\\)*")
        for cv in cv_sources:
            parts.append("• " + _escape_md(cv.get("filename", "без названия")))

    return "\n".join(parts)