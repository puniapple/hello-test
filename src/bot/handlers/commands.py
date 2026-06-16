"""Bot command handlers."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from src.agents.profile_agent import ProfileAgent
from src.services.claude import ClaudeService
from src.services.source_provisioning import provision_default_sources

from src.db.models import Profile, User, UserState, UserReaction
from src.db.session import async_session

router = Router()


WELCOME_TEXT = (
    'Привет! Это <a href="https://t.me/puniapple_speaks">Ульяна</a>. '
    'Я создала этого бота, потому что устала искать вакансию мечты на '
    'кладбище рынка труда.\n\n'
    'Что он умеет:\n\n'
    '🎯 <b>Понимает тебя глубже резюме.</b> Через короткий разговор разбирается, '
    'к чему лежит душа — даже если ты сам не уверен, кем хочешь быть дальше. '
    'Биздев, продукт, EdTech, M&A — бот ловит суть, а не просто ключевые слова.\n\n'
    '🔍 <b>Сам обходит источники.</b> Каждые 3 часа проверяет авторские карьерные '
    'тг-каналы и страницы топ-компаний (не использует агрегаторы — там слишком много мусора). '
    'Через AI оценивает каждую вакансию под твой профиль и присылает только релевантные.\n\n'
    '🎙 <b>Общайся как удобно.</b> Можно писать текстом, наговаривать голосовыми, '
    'присылать резюме в PDF — бот всё разберёт и вытащит фактологию сам.\n\n'
    '<b>С чего начать:</b> жми /edit_profile — соберём твой профиль через диалог.\n\n'
    '<b>Все команды:</b>\n'
    '/edit_profile — собрать или обновить профиль\n'
    '/show_profile — показать текущий профиль\n'
    '/done — выйти из режима редактирования\n'
    '/run_now — запустить поиск прямо сейчас\n'
    '/stats — твоя статистика\n'
    '/pause — поставить на паузу\n'
    '/resume — возобновить\n\n'
    'С багами и любым фидбэком — ко мне в личку <a href="https://t.me/puniapple">@puniapple</a>'
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
    await message.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)


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

@router.message(Command("run_now"))
async def cmd_run_now(message: Message) -> None:
    """Manually trigger a job search cycle for this user only.

    Useful for testing and on-demand fetch.
    """
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
    if user is None:
        await message.answer("Сначала напиши /start.")
        return

    await message.answer("🔄 Запускаю поиск... минут пять займёт.")

    from src.workers.job_search import _process_user
    try:
        result = await _process_user(message.bot, user)
        await message.answer(
            f"Готово!\n"
            f"Источников проверено: всё активное\n"
            f"Свежих вакансий найдено: {result['fetched']}\n"
            f"Прогнано через matcher: {result['matched']}\n"
            f"Отправлено тебе: {result['delivered']}"
        )
    except Exception as e:
        await message.answer(f"Ошибка во время поиска: {e}")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Personal stats: how many vacancies delivered, reactions breakdown."""
    from src.db.models import VacancyMatch

    async with async_session() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            await message.answer("Сначала напиши /start.")
            return

        match_result = await session.execute(
            select(VacancyMatch).where(VacancyMatch.user_id == user.id)
        )
        matches = list(match_result.scalars())

    if not matches:
        await message.answer("Пока ни одной вакансии не присылала.")
        return

    total = len(matches)
    liked = sum(1 for m in matches if m.user_reaction == UserReaction.liked)
    disliked = sum(1 for m in matches if m.user_reaction == UserReaction.disliked)
    applied = sum(1 for m in matches if m.user_reaction == UserReaction.applied)
    no_reaction = total - liked - disliked - applied
    avg_score = sum(m.match_score for m in matches) / total

    await message.answer(
        f"📊 Твоя статистика\n\n"
        f"Всего получено: {total}\n"
        f"Средний score: {avg_score:.1f}\n\n"
        f"👍 интересно: {liked}\n"
        f"📨 откликнулась: {applied}\n"
        f"👎 не моё: {disliked}\n"
        f"⏳ без реакции: {no_reaction}"
    )