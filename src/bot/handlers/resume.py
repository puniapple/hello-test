"""Inline button callback for adapted resume generation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery
from sqlalchemy import func, select

from src.db.models import Profile, ResumeUsage, User, VacancyMatch
from src.db.session import async_session
from src.services.resume import ResumeService
from src.sources.base import SourceType, Vacancy

log = structlog.get_logger(__name__)
router = Router()

# Free — 1 резюме за всё время жизни аккаунта.
# Pro/Grandfather — 3 в скользящем окне 24 часа.
FREE_LIFETIME_LIMIT = 1
PAID_ROLLING_LIMIT = 3
PAID_WINDOW_HOURS = 24


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


async def _count_lifetime(session, user_id: int) -> int:
    result = await session.execute(
        select(func.count(ResumeUsage.id)).where(ResumeUsage.user_id == user_id)
    )
    return result.scalar() or 0


async def _count_last_24h(session, user_id: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PAID_WINDOW_HOURS)
    result = await session.execute(
        select(func.count(ResumeUsage.id))
        .where(ResumeUsage.user_id == user_id)
        .where(ResumeUsage.generated_at >= cutoff)
    )
    return result.scalar() or 0


def _sanitize_filename(text: str, max_len: int = 60) -> str:
    """Убираем из имени файла символы, недопустимые в Windows/Telegram."""
    cleaned = "".join(c if c.isalnum() or c in " -_." else "_" for c in text)
    cleaned = "_".join(cleaned.split())
    return cleaned[:max_len].strip("_") or "resume"


@router.callback_query(F.data.startswith("resume:"))
async def handle_resume(callback: CallbackQuery) -> None:
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

        # ─── Проверка лимитов ───
        if plan == "free":
            already = await _count_lifetime(session, user.id)
            if already >= FREE_LIFETIME_LIMIT:
                await callback.message.answer(
                    "На бесплатном тарифе тебе доступно одно резюме, мы его уже написали.\n\n"
                    "В Pro я собираю по 3 резюме в день. Попробовать — /upgrade"
                )
                await callback.answer()
                return
        else:
            recent = await _count_last_24h(session, user.id)
            if recent >= PAID_ROLLING_LIMIT:
                await callback.message.answer(
                    f"На сегодня всё — три резюме за сутки я уже собрал. Возвращайся завтра.\n\n"
                    "Если срочно нужно ещё — напиши @puniapple."
                )
                await callback.answer()
                return

        # ─── Профиль ───
        profile = (await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()

        if profile is None or not profile.profile_data:
            await callback.message.answer(
                "Мне нужен твой профиль, чтобы собрать резюме.\n"
                "Заполни через /edit_profile."
            )
            await callback.answer()
            return

        vacancy = _reconstruct_vacancy(match.vacancy_data)

        await callback.answer("Собираю резюме…", show_alert=False)
        thinking_msg = await callback.message.answer(
            "📄 Собираю резюме под эту вакансию…\nЗайму 15-30 секунд."
        )

        try:
            service = ResumeService()
            result = await service.generate(profile.profile_data, vacancy)
        except Exception as e:
            log.error("resume_generation_failed", user_id=user.id, error=str(e))
            await thinking_msg.edit_text(
                "У меня не получилось собрать резюме. Попробуй ещё раз через минуту.\n"
                "Если повторяется — напиши @puniapple. Эта попытка не зачлась в лимит."
            )
            return

        # Логируем факт генерации
        session.add(ResumeUsage(
            user_id=user.id,
            vacancy_match_id=match_id,
        ))
        await session.commit()

        # Имя файла: Резюме_под_компанию_вакансию.docx
        company_part = _sanitize_filename(vacancy.company or "vakansiya", 30)
        role_part = _sanitize_filename(vacancy.title or "role", 40)
        prefix = "CV" if result.language == "en" else "Резюме"
        filename = f"{prefix}_{company_part}_{role_part}.docx"

        # Отправляем файл
        await thinking_msg.delete()
        await callback.message.answer_document(
            BufferedInputFile(result.docx_bytes, filename=filename),
        )

        # Диагностика в отдельном сообщении
        diag_lines = [
            f"📊 <b>Совпадение с вакансией: {result.match_percent}%</b>"
        ]
        if result.strong_alignment:
            diag_lines.append("")
            diag_lines.append("<b>Что сильно подходит:</b>")
            for item in result.strong_alignment[:3]:
                diag_lines.append(f"• {item}")
        if result.gaps:
            diag_lines.append("")
            diag_lines.append("<b>На что обратить внимание:</b>")
            for item in result.gaps[:2]:
                diag_lines.append(f"• {item}")

        # Хвост про лимит
        if plan == "free":
            diag_lines.append("")
            diag_lines.append(
                "<i>Это твоё единственное резюме на Free-тарифе. На Pro — 3 резюме в сутки. /upgrade</i>"
            )
        else:
            remaining = PAID_ROLLING_LIMIT - recent - 1
            diag_lines.append("")
            diag_lines.append(f"<i>Осталось на сегодня: {remaining} из {PAID_ROLLING_LIMIT}</i>")

        await callback.message.answer(
            "\n".join(diag_lines),
            parse_mode="HTML",
        )