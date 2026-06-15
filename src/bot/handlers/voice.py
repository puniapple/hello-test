"""Handler for voice messages."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select

from src.agents.profile_agent import ProfileAgent
from src.db.models import User, UserState
from src.db.session import async_session
from src.services.claude import ClaudeService
from src.services.whisper import WhisperService

router = Router()
logger = logging.getLogger(__name__)

MAX_VOICE_SECONDS = 300  # 5 min


@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    """Transcribe voice via Whisper and route as if it were a text message."""
    voice = message.voice

    if voice.duration > MAX_VOICE_SECONDS:
        await message.answer(
            f"Голосовое слишком длинное ({voice.duration} сек). "
            f"Лимит — 5 минут. Попробуй покороче или текстом."
        )
        return

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
            "Голосовые я пока понимаю только во время /edit_profile. "
            "Запусти команду и поговорим."
        )
        return

    notice = await message.answer("🎧 Расшифровываю...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # Download .ogg to a temp file
    file = await message.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        await message.bot.download_file(file.file_path, destination=tmp_path)

        whisper = WhisperService()
        try:
            transcript = await whisper.transcribe(tmp_path)
        except Exception as e:
            logger.exception("Whisper transcription failed")
            await notice.edit_text(f"Не получилось расшифровать: {e}")
            return
    finally:
        tmp_path.unlink(missing_ok=True)

    if not transcript:
        await notice.edit_text("Не услышала ничего разборчивого. Попробуй ещё раз.")
        return

    await notice.edit_text(f"🎧 Услышала: «{transcript}»")

    claude = ClaudeService()
    agent = ProfileAgent(claude=claude)
    reply = await agent.handle_message(user_id=user.id, user_text=transcript)
    await message.answer(reply.text)

    if reply.finalized:
        await message.answer(
            "✅ Профиль обновлён. /show_profile — посмотреть."
        )