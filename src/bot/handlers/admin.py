"""Admin commands for bot stats."""
from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import String, cast, distinct, func, select

from src.config import settings
from src.db.models import Profile, SeenVacancy, User, VacancyMatch
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
    today_utc = today_msk.astimezone(timezone.utc)
 
    async with async_session() as session:
        # ─── Основные метрики (как было) ───
        total = await session.scalar(select(func.count(User.id)))
 
        with_profile = await session.scalar(
            select(func.count(distinct(Profile.user_id)))
            .where(Profile.profile_data.isnot(None))
            .where(func.jsonb_typeof(Profile.profile_data) == "object")
            .where(cast(Profile.profile_data, String) != "{}")
        )
 
        active = await session.scalar(
            select(func.count(User.id))
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )
 
        # ─── Доставки за 3 календарных дня (UTC) ───
        # Сегодня — неполный день, отдельно
        delivered_today_total = await session.scalar(
            select(func.count(VacancyMatch.id))
            .where(VacancyMatch.sent_at >= today_utc)
        )
        delivered_today_users = await session.scalar(
            select(func.count(distinct(VacancyMatch.user_id)))
            .where(VacancyMatch.sent_at >= today_utc)
        )
 
        deliveries_by_day: list[tuple[str, int, int]] = []
        for days_ago in range(1, 4):
            day_start = today_utc - timedelta(days=days_ago)
            day_end = today_utc - timedelta(days=days_ago - 1)
 
            total_vacancies = await session.scalar(
                select(func.count(VacancyMatch.id))
                .where(VacancyMatch.sent_at >= day_start)
                .where(VacancyMatch.sent_at < day_end)
            )
            uniq_users = await session.scalar(
                select(func.count(distinct(VacancyMatch.user_id)))
                .where(VacancyMatch.sent_at >= day_start)
                .where(VacancyMatch.sent_at < day_end)
            )
            deliveries_by_day.append((
                day_start.strftime("%d.%m"),
                total_vacancies or 0,
                uniq_users or 0,
            ))
 
        # ─── Динамика юзеров: новые + в матчинге за 3 календарных дня ───
        new_today = await session.scalar(
            select(func.count(User.id))
            .where(User.created_at >= today_utc)
        )
        matching_today = await session.scalar(
            select(func.count(distinct(SeenVacancy.user_id)))
            .where(SeenVacancy.sent_at >= today_utc)
        )
 
        users_by_day: list[tuple[str, int, int]] = []
        for days_ago in range(1, 4):
            day_start = today_utc - timedelta(days=days_ago)
            day_end = today_utc - timedelta(days=days_ago - 1)
 
            new_count = await session.scalar(
                select(func.count(User.id))
                .where(User.created_at >= day_start)
                .where(User.created_at < day_end)
            )
            matching_count = await session.scalar(
                select(func.count(distinct(SeenVacancy.user_id)))
                .where(SeenVacancy.sent_at >= day_start)
                .where(SeenVacancy.sent_at < day_end)
            )
            users_by_day.append((
                day_start.strftime("%d.%m"),
                new_count or 0,
                matching_count or 0,
            ))
 
    # ─── Форматирование ───
    lines = [
        f"📊 <b>Admin stats</b> — {now_msk.strftime('%Y-%m-%d %H:%M MSK')}",
        "",
        f"👥 Всего юзеров: <b>{total or 0}</b>",
        f"📝 С профилем: <b>{with_profile or 0}</b>",
        f"🚀 Активных (поиск идёт): <b>{active or 0}</b>",
        "",
        "📬 <b>Доставки (вакансий / юзеров)</b>",
        f"  сегодня: <b>{delivered_today_total or 0}</b> / <b>{delivered_today_users or 0}</b>",
    ]
    for date_str, vacs, users in deliveries_by_day:
        lines.append(f"  {date_str}: <b>{vacs}</b> / <b>{users}</b>")
 
    lines.extend([
        "",
        "📈 <b>Юзеры (новых / в матчинге)</b>",
        f"  сегодня: <b>{new_today or 0}</b> / <b>{matching_today or 0}</b>",
    ])
    for date_str, new, matching in users_by_day:
        lines.append(f"  {date_str}: <b>{new}</b> / <b>{matching}</b>")
 
    await message.answer("\n".join(lines), parse_mode="HTML")