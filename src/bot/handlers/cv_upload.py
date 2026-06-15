"""Handler for PDF resume uploads."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from src.agents.profile_agent import ProfileAgent
from src.db.models import User, UserState
from src.db.session import async_session
from src.services.claude import ClaudeService, build_pdf_content_block

router = Router()
logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB

# In-memory cache: callback_data -> (file_id, filename)
# Telegram callback_data is limited to 64 bytes, so we store
# the actual data here and pass only a short token.
PENDING_UPLOADS: dict[str, tuple[str, str]] = {}


def _build_choice_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 Найти под это резюме",
                    callback_data=f"cv:find:{token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Просто добавить в профиль",
                    callback_data=f"cv:add:{token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✕ Отмена",
                    callback_data=f"cv:cancel:{token}",
                )
            ],
        ]
    )


@router.message(F.document)
async def handle_document(message: Message) -> None:
    """Route PDF documents based on user state."""
    doc = message.document
    if not doc.mime_type or "pdf" not in doc.mime_type.lower():
        await message.answer("Я пока умею читать только PDF. Пришли резюме в PDF.")
        return

    if doc.file_size and doc.file_size > MAX_PDF_BYTES:
        await message.answer(
            f"Файл великоват ({doc.file_size // 1024 // 1024} МБ). "
            f"Лимит — 10 МБ. Попробуй сжать PDF."
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

    if user.state == UserState.editing_profile:
        await _feed_pdf_to_agent(message, user.id, doc.file_id, doc.file_name or "resume.pdf")
        return

    # User is not editing — offer choice
    token = doc.file_unique_id[:32]
    PENDING_UPLOADS[token] = (doc.file_id, doc.file_name or "resume.pdf")

    await message.answer(
        f"Получила резюме *{doc.file_name or 'resume.pdf'}*. Что с ним сделать?",
        reply_markup=_build_choice_keyboard(token),
        parse_mode="Markdown",
    )


async def _download_pdf(message: Message, file_id: str) -> bytes:
    """Download a Telegram document and return its bytes."""
    file = await message.bot.get_file(file_id)
    buffer = await message.bot.download_file(file.file_path)
    return buffer.read()


async def _feed_pdf_to_agent(
    message: Message,
    user_id: int,
    file_id: str,
    filename: str,
) -> None:
    """When user is mid-editing, pass PDF straight to the profile agent."""
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    await message.answer(f"📄 Разбираю резюме *{filename}*, минутку...", parse_mode="Markdown")
    pdf_bytes = await _download_pdf(message, file_id)
    pdf_block = build_pdf_content_block(pdf_bytes, filename=filename)

    claude = ClaudeService()
    agent = ProfileAgent(claude=claude)
    reply = await agent.handle_message(
        user_id=user_id,
        user_text=f"Я прислал резюме '{filename}'. Разбери его и обнови профиль.",
        extra_content_blocks=[pdf_block],
    )
    await message.answer(reply.text)
    if reply.finalized:
        await message.answer("✅ Профиль обновлён. /show_profile")


@router.callback_query(F.data.startswith("cv:cancel:"))
async def callback_cancel(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 2)[2]
    PENDING_UPLOADS.pop(token, None)
    await callback.message.edit_text("Окей, отменили.")
    await callback.answer()


@router.callback_query(F.data.startswith("cv:add:"))
async def callback_add_to_profile(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 2)[2]
    pending = PENDING_UPLOADS.pop(token, None)
    if pending is None:
        await callback.answer("Это резюме уже обработано или устарело.", show_alert=True)
        return

    file_id, filename = pending
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
    if user is None:
        await callback.answer("Сначала напиши /start.", show_alert=True)
        return

    await callback.message.edit_text("📄 Разбираю резюме, минутку...")
    await callback.message.bot.send_chat_action(
        chat_id=callback.message.chat.id, action="typing"
    )

    try:
        pdf_bytes = await _download_pdf(callback.message, file_id)
        pdf_block = build_pdf_content_block(pdf_bytes, filename=filename)

        claude = ClaudeService()
        agent = ProfileAgent(claude=claude)
        reply = await agent.handle_message(
            user_id=user.id,
            user_text=(
                f"Юзер прислал резюме '{filename}' вне режима редактирования. "
                "Разбери его, обнови фактологические поля профиля через "
                "update_profile_field (expertise, current_role_summary, languages, "
                "seniority, industries из прошлого опыта). Добавь запись через "
                "add_cv_source. Ответь юзеру одним коротким сообщением — что "
                "именно ты добавила в профиль. НЕ задавай уточняющих вопросов и "
                "НЕ вызывай finalize_editing — это не сессия редактирования."
            ),
            extra_content_blocks=[pdf_block],
            persist_user_message=False,
        )
    except Exception as e:
        logger.exception("CV processing failed")
        await callback.message.answer(f"Не получилось разобрать резюме: {e}")
        await callback.answer()
        return

    await callback.message.answer(reply.text)
    await callback.message.answer(
        "Информация из резюме теперь учитывается в обычном поиске. /show_profile"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cv:find:"))
async def callback_find_under_cv(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 2)[2]
    pending = PENDING_UPLOADS.pop(token, None)
    if pending is None:
        await callback.answer("Это резюме уже обработано или устарело.", show_alert=True)
        return

    file_id, filename = pending
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
    if user is None:
        await callback.answer("Сначала напиши /start.", show_alert=True)
        return

    await callback.message.edit_text("📄 Разбираю резюме и добавляю в профиль...")
    await callback.message.bot.send_chat_action(
        chat_id=callback.message.chat.id, action="typing"
    )

    try:
        pdf_bytes = await _download_pdf(callback.message, file_id)
        pdf_block = build_pdf_content_block(pdf_bytes, filename=filename)

        claude = ClaudeService()
        agent = ProfileAgent(claude=claude)
        reply = await agent.handle_message(
            user_id=user.id,
            user_text=(
                f"Юзер прислал резюме '{filename}' и хочет, чтобы под него "
                "сделали разовый поиск вакансий. Разбери резюме, обнови "
                "фактологические поля профиля через update_profile_field, "
                "добавь запись через add_cv_source. Ответь юзеру одним "
                "коротким сообщением — что добавила в профиль. НЕ задавай "
                "уточняющих вопросов и НЕ вызывай finalize_editing."
            ),
            extra_content_blocks=[pdf_block],
            persist_user_message=False,
        )
    except Exception as e:
        logger.exception("CV processing failed")
        await callback.message.answer(f"Не получилось разобрать резюме: {e}")
        await callback.answer()
        return

    await callback.message.answer(reply.text)
    await callback.message.answer(
        "🔍 Разовый поиск под резюме появится, когда подключим источники вакансий "
        "(следующие фазы). Пока резюме просто добавлено в профиль — оно будет "
        "учитываться при обычном поиске."
    )
    await callback.answer()