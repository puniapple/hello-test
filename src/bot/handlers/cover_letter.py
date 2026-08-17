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

from src.services.access import (
    check_daily_limit,
    has_access,
    DAILY_LIMITS,
)

log = structlog.get_logger(__name__)
router = Router()

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

        # Гейт подписки: только для Free
        if plan == "free":
            from src.services.subscription import (
                is_required_channel_configured,
                is_subscribed,
                send_gate_prompt,
            )
            if is_required_channel_configured():
                subscribed = await is_subscribed(callback.bot, user.telegram_id)
                if not subscribed:
                    await send_gate_prompt(callback.bot, callback.from_user.id)
                    await callback.answer()
                    return

        # Access gate — сначала проверяем что у юзера вообще есть доступ
        if not has_access(user):
            await callback.answer()
            await callback.message.answer(
                "Бот работает только по подписке. Запустить поиск можно от 349₽ 👇🏼",
                reply_markup=_paywall_keyboard(),
            )
            return

        # Лимит — 2 в сутки для всех, у кого есть доступ
        allowed, used, limit = await check_daily_limit(session, user.id, "cover_letter")
        if not allowed:
            await callback.message.answer(
                f"На сегодня всё — {limit} сопроводительных за сутки я уже написал. "
                "Возвращайся завтра.\n\n"
                "Если срочно нужно ещё — напиши @puniapple."
            )
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
                "Что-то у меня не получилось сгенерировать сопроводительное. Попробуй ещё раз через минуту — эта попытка не зачлась в дневной лимит.\n\nЕсли повторяется — напиши @puniapple."
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
        footer = "\n\n_" + " · ".join(footer_parts) + "_"

        # Отправляем письмо c шапкой сверху и футером
        await thinking_msg.delete()
        header = "Готово, лови сопроводительное — под эту вакансию и твой профиль:\n\n"
        await callback.message.answer(header + letter)
        await callback.message.answer(footer, parse_mode="Markdown")

def _paywall_keyboard():
    """Reuse из middleware или локально — две кнопки на Tribute."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from src.config import settings
    buttons = []
    if settings.tribute_subscription_weekly_url:
        buttons.append([InlineKeyboardButton(
            text="📅 Неделя — 349₽",
            url=settings.tribute_subscription_weekly_url,
        )])
    if settings.tribute_subscription_monthly_url:
        buttons.append([InlineKeyboardButton(
            text="💎 Месяц — 990₽",
            url=settings.tribute_subscription_monthly_url,
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)