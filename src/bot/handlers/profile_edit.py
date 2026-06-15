"""Handler for free-text messages during profile editing."""

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select

from src.agents.profile_agent import ProfileAgent
from src.db.models import User, UserState
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

    await message.answer(reply.text)

    if reply.finalized:
        await message.answer(
            "✅ Профиль обновлён. Посмотреть: /show_profile\n"
            "Подключим источники вакансий чуть позже."
        )