"""Inline button callback for cover letter generation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from src.db.models import CoverLetterUsage, Profile, User, VacancyMatch
from src.db.session import async_session
from src.services.cover_letter import CoverLetterService
from src.sources.base import SourceType, Vacancy

log = structlog.get_logger(__name__)
router = Router()

# Лимиты писем за скользящее окно 24ч. Grandfather = Pro.
COVER_LETTER_LIMITS = {
    "free": 1,
    "pro": 5,
    "grandfather": 5,
}

# Fallback, если plan неизвестного значения
DEFAULT_LIMIT = 1

# Максимальная длина одного Telegram-сообщения — 4096 символов.
# Сопроводительное 130-220 слов = ~800-1400 символов, легко влезает.
TELEGRAM_MESSAGE_LIMIT = 4096


def _reconstruct_vacancy(vacancy_data: dict) -> Vacancy:
    """VacancyMatch хранит vacancy_data как JSONB — восстанавливаем Vacancy-объект."""
    return Vacancy(
        external_id=vacancy_data.get("external_id", ""),
        source_type=SourceType.career_site,  # тип не критичен для генерации письма
        title=vacancy_data.get("title", ""),
        company=vacancy_data.get("company", ""),
        url=vacancy_data.get("url", ""),
        description=vacancy_data.get("description", ""),
        salary=vacancy_data.get("salary"),
        location=vacancy_data.get("location"),
        published_at=None,
        raw=vacancy_data.get("raw", {}),
    )


async def _count_recent_letters(session, user_id: int) -> int:
    """COUNT сгенерированных писем за последние 24 часа."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await session.execute(
        select(func.count(CoverLetterUsage.id))
        .where(CoverLetterUsage.user_id == user_id)
        .where(CoverLetterUsage.generated_at >= cutoff)
    )
    return result.scalar() or 0


@router.callback_query(F.data.startswith("cover:"))
async def handle_cover_letter(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Что-то не так с кнопкой", show_alert=False)
        return

    _, action, match_id_str = parts
    if action != "generate":
        await callback.answer("Неизвестное действие", show_alert=False)
        return

    try:
        match_id = int(match_id_str)
    except ValueError:
        await callback.answer("Неверный ID вакансии", show_alert=False)
        return

    async with async_session() as session:
        match = (await session.execute(
            select(VacancyMatch).where(VacancyMatch.id == match_id)
        )).scalar_one_or_none()

        if match is None:
            await callback.answer("Вакансия не найдена", show_alert=False)
            return

        user = (await session.execute(
            select(User).where(User.id == match.user_id)
        )).scalar_one_or_none()

        if user is None or user.telegram_id != callback.from_user.id:
            # Защита — юзер жмёт кнопку под чужой вакансией
            await callback.answer("Это не твоя вакансия", show_alert=False)
            return

        # Проверка лимита
        plan = (user.plan or "free").lower()
        limit = COVER_LETTER_LIMITS.get(plan, DEFAULT_LIMIT)
        used = await _count_recent_letters(session, user.id)

        if used >= limit:
            if plan == "free":
                text = (
                    "На Free тарифе доступно 1 сопроводительное в день.\n"
                    "Следующее можно будет написать через сутки.\n\n"
                    "На Pro — 5 писем в день. /upgrade"
                )
            else:
                text = (
                    f"Лимит {limit} писем в день исчерпан.\n"
                    "Следующее можно будет написать через сутки."
                )
            await callback.message.answer(text)
            await callback.answer()
            return

        # Достаём профиль
        profile = (await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()

        if profile is None or not profile.profile_data:
            await callback.message.answer(
                "Мне нужен твой профиль, чтобы писать сопроводительные.\n"
                "Заполни через /edit_profile."
            )
            await callback.answer()
            return

        vacancy_data = match.vacancy_data
        vacancy = _reconstruct_vacancy(vacancy_data)

        # Сообщение "генерирую" — юзер видит, что процесс пошёл (Sonnet отвечает 5-10 сек)
        await callback.answer("Пишу сопроводительное…", show_alert=False)
        thinking_msg = await callback.message.answer("✍️ Пишу…")

        try:
            service = CoverLetterService()
            letter = await service.generate(profile.profile_data, vacancy)
        except Exception as e:
            log.error("cover_letter_failed", user_id=user.id, error=str(e))
            await thinking_msg.edit_text(
                "У меня не получилось написать сопроводительное. Попробуй ещё раз.\n"
                "Если повторяется — напиши @puniapple."
            )
            return

        # Логируем факт генерации (для rate limit и статистики)
        session.add(CoverLetterUsage(
            user_id=user.id,
            vacancy_match_id=match_id,
        ))
        await session.commit()

        # Обрезаем на всякий случай — Telegram лимит 4096
        letter = letter[:TELEGRAM_MESSAGE_LIMIT]

        remaining = limit - used - 1
        footer_parts = [f"Осталось на сегодня: {remaining} из {limit}"]
        if plan == "free" and remaining == 0:
            footer_parts.append("На Pro — 5 писем в день. /upgrade")
        footer = "\n\n_" + " · ".join(footer_parts) + "_"

        # Отправляем письмо + подпись отдельным сообщением
        # (не редактируем "Пишу…", потому что письмо может быть длинным)
        await thinking_msg.delete()
        await callback.message.answer(letter)
        await callback.message.answer(footer, parse_mode="Markdown")