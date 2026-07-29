"""Inline button handler for score breakdown."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from html import escape

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from src.db.models import Profile, ScoreBreakdown, User, VacancyMatch
from src.db.session import async_session
from src.services.score_breakdown import ScoreBreakdownService
from src.sources.base import SourceType, Vacancy

log = structlog.get_logger(__name__)
router = Router()

# Tribute-ссылка на подписку (из твоей памяти по проекту)
TRIBUTE_SUBSCRIPTION_URL = os.getenv(
    "TRIBUTE_SUBSCRIPTION_URL",
    "https://t.me/tribute/app?startapp=sZWl",
)


def _reconstruct_vacancy(vacancy_data: dict) -> Vacancy:
    return Vacancy(
        external_id=vacancy_data.get("external_id", ""),
        source_type=SourceType.career_site,
        title=vacancy_data.get("title", ""),
        company=vacancy_data.get("company", ""),
        url=vacancy_data.get("url", ""),
        description=vacancy_data.get("description", ""),
        salary=vacancy_data.get("salary"),
        location=vacancy_data.get("location"),
        published_at=None,
        raw=vacancy_data.get("raw", {}),
    )


def _score_to_percent(match_score: float) -> int:
    """Скор 0-10 → проценты 0-100."""
    return max(0, min(100, int(round(match_score * 10))))


def _render_breakdown_free(percent: int, pros: list[str], gaps: list[str]) -> str:
    """Разбор для Free — без verdict, только % + списки."""
    lines = [f"<b>Вакансия подходит тебе на {percent}%</b>", ""]

    if pros:
        lines.append("<b>Что совпадает:</b>")
        for p in pros:
            lines.append(f"— {escape(p)}")
        lines.append("")

    if gaps:
        lines.append("<b>На что обратить внимание:</b>")
        for g in gaps:
            lines.append(f"— {escape(g)}")
        lines.append("")

    # F-CTA — конверсия на пике впечатления
    lines.append(
        "<i>Это был твой пробный разбор — по одному на Free. "
        "В Pro я так разбираю каждую вакансию. /upgrade</i>"
    )
    return "\n".join(lines)


def _render_breakdown_pro(
    percent: int, pros: list[str], gaps: list[str], verdict: str
) -> str:
    """Разбор для Pro/Grandfather — со скором и verdict."""
    lines = [
        "🔍 <b>Разбор соответствия</b>",
        "",
        f"<b>Вакансия подходит тебе на {percent}%</b>",
        "",
        escape(verdict),
        "",
    ]

    if pros:
        lines.append("<b>Что совпадает:</b>")
        for p in pros:
            lines.append(f"— {escape(p)}")
        lines.append("")

    if gaps:
        lines.append("<b>На что обратить внимание:</b>")
        for g in gaps:
            lines.append(f"— {escape(g)}")

    return "\n".join(lines).rstrip()


def _paywall_keyboard() -> InlineKeyboardMarkup:
    """Кнопка ведёт на Tribute-страницу подписки (URL-кнопка).

    Не через callback upgrade:start (такого handler'а нет),
    а напрямую на Tribute mini-app.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💎 Подключить Pro", url=TRIBUTE_SUBSCRIPTION_URL),
    ]])


PAYWALL_TEXT = (
    "Подключи Pro, чтобы узнать, в чём ты подходишь компании, а в чём есть пробелы."
)

FALLBACK_TEXT = (
    "Задумался и потерял мысль. Попробуй через минуту."
)

THINKING_TEXT = "⏳ Смотрю внимательнее…"


@router.callback_query(F.data.startswith("breakdown:"))
async def handle_breakdown(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] != "generate":
        await callback.answer("Что-то не так с кнопкой", show_alert=False)
        return

    try:
        match_id = int(parts[2])
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
            await callback.answer("Это не твоя вакансия", show_alert=False)
            return

        plan = (user.plan or "free").lower()
        is_paid = plan in ("pro", "grandfather")

        # 1. Проверка БД-кеша по match_id
        existing = (await session.execute(
            select(ScoreBreakdown).where(ScoreBreakdown.match_id == match_id)
        )).scalar_one_or_none()

        if existing is not None:
            # Есть сохранённый — показываем без генерации, независимо от plan.
            # Триал у Free привязан к вакансии: повторный клик на ту же не тратит.
            percent = existing.score
            data = existing.breakdown_json or {}
            pros = data.get("pros", []) or []
            gaps = data.get("gaps", []) or []
            verdict = data.get("verdict", "") or ""

            if is_paid:
                text = _render_breakdown_pro(percent, pros, gaps, verdict)
            else:
                text = _render_breakdown_free(percent, pros, gaps)

            await callback.answer()
            await callback.message.answer(text, parse_mode="HTML")
            return

        # 2. Free с израсходованным триалом → pay-wall (не генерим)
        if not is_paid and user.free_breakdown_used_at is not None:
            await callback.answer()
            await callback.message.answer(
                PAYWALL_TEXT,
                reply_markup=_paywall_keyboard(),
            )
            return

        # 3. Профиль
        profile = (await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()
        if profile is None or not profile.profile_data:
            await callback.message.answer(
                "Мне нужен твой профиль, чтобы разобрать вакансию. Заполни через /edit_profile."
            )
            await callback.answer()
            return

        vacancy = _reconstruct_vacancy(match.vacancy_data)

        # 4. Промежуточное сообщение
        await callback.answer()
        thinking_msg = await callback.message.answer(THINKING_TEXT)

        # 5. Генерация
        try:
            service = ScoreBreakdownService()
            result = await service.generate(
                profile_data=profile.profile_data,
                vacancy=vacancy,
                haiku_score=match.match_score or 0.0,
            )
        except Exception as e:
            log.error(
                "breakdown_generation_failed",
                user_id=user.id,
                match_id=match_id,
                error=str(e),
            )
            try:
                await thinking_msg.edit_text(FALLBACK_TEXT)
            except TelegramAPIError:
                await callback.message.answer(FALLBACK_TEXT)
            # Триал НЕ тратим при фейле
            return

        # 6. Сохраняем в БД
        percent = _score_to_percent(match.match_score or 0.0)
        session.add(ScoreBreakdown(
            match_id=match_id,
            user_id=user.id,
            breakdown_json={
                "pros": result.pros,
                "gaps": result.gaps,
                "verdict": result.verdict,
                "verdict_level": result.verdict_level,
            },
            score=percent,
            model_used=result.model_used,
        ))

        # 7. Если это Free-триал — помечаем израсходованным
        if not is_paid:
            user.free_breakdown_used_at = datetime.now(timezone.utc)

        await session.commit()

        # 8. Рендер + отправка
        if is_paid:
            text = _render_breakdown_pro(percent, result.pros, result.gaps, result.verdict)
        else:
            text = _render_breakdown_free(percent, result.pros, result.gaps)

        try:
            await thinking_msg.delete()
        except TelegramAPIError:
            pass

        await callback.message.answer(text, parse_mode="HTML")