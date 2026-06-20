"""Handler for free-text messages during profile editing."""

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select

from src.agents.profile_agent import ProfileAgent
from src.db.models import User, UserState, Profile
from src.db.session import async_session
from src.services.claude import ClaudeService

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_in_editing(message: Message) -> None:
    """Route plain-text messages to the profile agent if user is in editing mode."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

    if user is None:
        await message.answer("Сначала напиши /start.")
        return

    if user.state != UserState.editing_profile:
        await message.answer(
            "Чтобы я тебя понимал — запусти /edit_profile для сборки профиля "
            "или используй команды из /help."
        )
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    claude = ClaudeService()
    agent = ProfileAgent(claude=claude)
    reply = await agent.handle_message(user_id=user.id, user_text=message.text)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    ready = user.profile_ready_for_search if hasattr(user, "profile_ready_for_search") else False
    if ready:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить диалог (/done)", callback_data="profile:done")],
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Уже хватит, начать поиск!", callback_data="profile:start_search")],
            [InlineKeyboardButton(text="✅ Завершить диалог (/done)", callback_data="profile:done")],
        ])

    await message.answer(reply.text, reply_markup=keyboard)

    if reply.finalized:
        await message.answer(
            "✅ Профиль обновлён. Посмотреть: /show_profile\n"
            "Подключим источники вакансий чуть позже."
        )

from aiogram import F
from aiogram.types import CallbackQuery


@router.callback_query(F.data == "profile:start_search")
async def handle_start_search(callback: CallbackQuery) -> None:
    """Юзер нажал 'хватит, начать поиск' — флипаем флаг и запускаем первый цикл."""
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )).scalar_one_or_none()
        if user is None:
            await callback.answer("Сначала /start", show_alert=True)
            return

        if user.profile_ready_for_search:
                await callback.answer("Поиск уже запущен", show_alert=False)
                return

        # Проверяем готовность профиля
        from src.services.profile_validation import is_profile_ready

        profile_result = await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )
        profile = profile_result.scalar_one_or_none()
        ready, reason = is_profile_ready(profile.profile_data if profile else None)

        if not ready:
            await callback.answer(reason, show_alert=True)
            return

        user.profile_ready_for_search = True
        await session.commit()

    await callback.answer("Запускаю поиск! Это займёт пару минут.", show_alert=False)
    await callback.message.answer(
        "🚀 Поиск запущен.\n\n"
        "Бот будет автоматически проверять источники каждые 3 часа и присылать "
        "релевантные вакансии. Ты можешь продолжать дополнять профиль в любой момент — "
        "новые ответы попадут в учёт.\n\n"
        "Если захочешь запустить поиск прямо сейчас — нажми /run_now."
    )

    # Запускаем первый цикл в фоне
    from src.workers.job_search import _process_user
    import asyncio
    asyncio.create_task(_process_user(callback.bot, user))


@router.callback_query(F.data == "profile:done")
async def handle_done(callback: CallbackQuery) -> None:
    """Юзер хочет выйти из режима редактирования."""
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )).scalar_one_or_none()
        if user is None:
            await callback.answer("Сначала /start", show_alert=True)
            return

        user.state = UserState.idle
        await session.commit()

    await callback.answer("Режим редактирования завершён", show_alert=False)
    await callback.message.answer(
        "✅ Готово. Если ещё не нажал кнопку 'Начать поиск' — бот вакансии присылать не будет.\n\n"
        "Вернуться к редактированию: /edit_profile\n"
        "Запустить поиск сейчас: /run_now"
    )
