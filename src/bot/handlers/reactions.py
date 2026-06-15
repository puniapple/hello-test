"""Inline button callbacks for vacancy reactions."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from src.db.models import UserReaction, VacancyMatch
from src.db.session import async_session

router = Router()


REACTION_LABELS = {
    "liked": "👍 Отмечено как интересное",
    "disliked": "👎 Отмечено как 'не моё'",
    "applied": "📨 Отмечено как 'откликнулась'",
}


@router.callback_query(F.data.startswith("react:"))
async def handle_reaction(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Что-то не так с кнопкой", show_alert=False)
        return

    _, reaction_str, match_id_str = parts
    try:
        match_id = int(match_id_str)
        reaction = UserReaction(reaction_str)
    except (ValueError, KeyError):
        await callback.answer("Неизвестная реакция", show_alert=False)
        return

    async with async_session() as session:
        result = await session.execute(
            select(VacancyMatch).where(VacancyMatch.id == match_id)
        )
        match = result.scalar_one_or_none()
        if match is None:
            await callback.answer("Вакансия не найдена", show_alert=False)
            return
        match.user_reaction = reaction
        await session.commit()

    label = REACTION_LABELS.get(reaction_str, "Записала")
    await callback.answer(label, show_alert=False)