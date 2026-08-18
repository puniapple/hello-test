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
            "Если хочешь обновить профиль — /edit_profile\nВсе команды — /help"
        )
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    claude = ClaudeService()
    agent = ProfileAgent(claude=claude)
    reply = await agent.handle_message(user_id=user.id, user_text=message.text)
    from src.services.profile_activation import try_auto_activate_search
    await try_auto_activate_search(message.bot, user.id)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    # Показываем кнопку "начать поиск" только если поиск ещё не активирован
    # И профиль реально готов (достаточно полей заполнено)
    from sqlalchemy import select
    from src.db.models import Profile
    from src.services.profile_validation import is_profile_ready
    from src.db.session import async_session

    already_active = user.profile_ready_for_search if hasattr(user, "profile_ready_for_search") else False

    profile_is_ready = False
    if not already_active:
        async with async_session() as check_session:
            p = (await check_session.execute(
                select(Profile).where(Profile.user_id == user.id)
            )).scalar_one_or_none()
            if p:
                profile_is_ready, _ = is_profile_ready(p.profile_data or {})

    if already_active:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить диалог (/done)", callback_data="profile:done")],
        ])
    elif profile_is_ready:
        # Профиль готов, поиск ещё не активирован — предлагаем запустить
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Уже хватит, начать поиск!", callback_data="profile:start_search")],
            [InlineKeyboardButton(text="✅ Завершить диалог (/done)", callback_data="profile:done")],
        ])
    else:
        # Профиль ещё не заполнен — только завершить диалог, без активации
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
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

        if user.profile_ready_for_search:
            await callback.answer("Уже в поиске 🚀 Первая подборка скоро придёт.", show_alert=False)
            return
        user.profile_ready_for_search = True
        await session.commit()

    await callback.answer("Запускаю поиск! Это займёт пару минут.", show_alert=False)
    # Определяем тариф для персонализации текста
    if user.plan == "grandfather" or (user.plan == "pro" and user.subscription_status == "pro_active"):
        activation_text = (
            "Готово, ты в поиске 🚀\n\n"
            "Я буду присылать подборки несколько раз в день — до 5 самых подходящих вакансий за раз. Под каждой две кнопки:\n\n"
            "✍️ Написать сопроводительное — до 5 в день\n"
            "📝 Собрать резюме — до 3 в день\n\n"
            "Если захочешь скорректировать запрос — возвращайся в /edit_profile"
        )
    else:
        activation_text = (
            "Готово, ты в поиске 🚀\n\n"
            "Я буду присылать подборку раз в день — до 3 самых подходящих вакансий. Под каждой две кнопки:\n\n"
            "✍️ Написать сопроводительное — тебе доступно одно в день\n"
            "📝 Собрать резюме — доступно всего одно резюме\n\n"
            "Хочешь больше? На Pro я работаю несколько раз в день, присылаю до 5 вакансий за раз, пишу до 5 сопроводительных и собираю до 3 резюме в день. От 349₽ в неделю — /upgrade"
        )
    await callback.message.answer(activation_text)

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
    # После /done проверяем готов ли профиль
    is_ready = user.profile_ready_for_search
    if is_ready:
        text = (
            "Окей, останавливаемся ✅\n\n"
            "Профиль выглядит готовым к поиску. Обновить или дополнить — /edit_profile"
        )
    else:
        text = (
            "Окей, останавливаемся ✅\n\n"
            "В профиле пока маловато информации для поиска — давай вернёмся, когда будет время. Я буду тут.\n\n"
            "Когда будешь готов — /edit_profile"
        )
    await callback.message.answer(text)
