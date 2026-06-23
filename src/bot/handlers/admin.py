"""Admin commands for bot stats."""
from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from src.config import settings
from src.db.models import Profile, User, VacancyMatch
from src.db.session import async_session

router = Router()
MSK = timezone(timedelta(hours=3))


def _is_admin(user_id: int) -> bool:
    ids = getattr(settings, "admin_telegram_ids", None) or []
    if isinstance(ids, str):
        ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    return user_id in [int(x) for x in ids]


@router.message(Command("admin_stats"))
async def admin_stats(message: Message) -> None:
    if not message.from_user or not _is_admin(message.from_user.id):
        return  # тихо игнорим не-админов

    now_msk = datetime.now(MSK)
    today_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    yday_msk = today_msk - timedelta(days=1)
    today_utc = today_msk.astimezone(timezone.utc)
    yday_utc = yday_msk.astimezone(timezone.utc)

    async with async_session() as session:
        total = await session.scalar(select(func.count(User.id)))

        with_profile = await session.scalar(
            select(func.count(func.distinct(Profile.user_id)))
            .where(Profile.profile_data.isnot(None))
            .where(func.jsonb_typeof(Profile.profile_data) == "object")
            .where(func.cast(Profile.profile_data, type_=__import__("sqlalchemy").String) != "{}")
        )

        active = await session.scalar(
            select(func.count(User.id))
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )

        delivered_yday = await session.scalar(
            select(func.count(func.distinct(VacancyMatch.user_id)))
            .where(VacancyMatch.sent_at >= yday_utc)
            .where(VacancyMatch.sent_at < today_utc)
        )

        delivered_today = await session.scalar(
            select(func.count(func.distinct(VacancyMatch.user_id)))
            .where(VacancyMatch.sent_at >= today_utc)
        )

    text = (
        f"📊 <b>Admin stats</b> — {now_msk.strftime('%Y-%m-%d %H:%M MSK')}\n\n"
        f"👥 Всего юзеров: <b>{total or 0}</b>\n"
        f"📝 С профилем: <b>{with_profile or 0}</b>\n"
        f"🚀 Активных (поиск идёт): <b>{active or 0}</b>\n"
        f"📬 Получили вакансии вчера: <b>{delivered_yday or 0}</b>\n"
        f"📬 Получили вакансии сегодня: <b>{delivered_today or 0}</b>\n"
    )
    await message.answer(text, parse_mode="HTML")