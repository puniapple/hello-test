"""Access control and daily limits for the paid-only model.

Единая точка контроля доступа и лимитов. Заменяет Free/Pro/Grandfather
дифференциацию — теперь две категории:
    - has_access = True: подписка активна ИЛИ grandfather → лимиты 3/2/2/2
    - has_access = False: paywall

Лимиты одинаковые для всех, у кого есть доступ. Grandfather отличается
только тем, что доступ есть без подписки. Безлимита нет ни у кого.

Окно: rolling 24 часа (согласовано с существующими счётчиками
в resume.py и cover_letter.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    CoverLetterUsage,
    ResumeUsage,
    ScoreBreakdown,
    User,
    VacancyMatch,
)

# ─────────────────────────────────────────────────────────────
# Единая ветка лимитов (rolling 24 часа)
# ─────────────────────────────────────────────────────────────
DAILY_LIMITS = {
    "vacancies": 3,       # доставки вакансий в день
    "breakdown": 2,       # разборов соответствия в день
    "resume": 2,          # резюме под вакансию в день
    "cover_letter": 2,    # сопроводительных в день
}

ROLLING_WINDOW_HOURS = 24

LimitKind = Literal["vacancies", "breakdown", "resume", "cover_letter"]


# ─────────────────────────────────────────────────────────────
# Доступ
# ─────────────────────────────────────────────────────────────
def has_access(user: User) -> bool:
    """Единая точка проверки доступа к боту.

    Доступ есть если:
    - Grandfather (историческая бесплатная роль, без подписки)
    - ИЛИ подписка активна и не истекла

    Всё остальное — paywall.
    """
    if user is None:
        return False

    plan = (user.plan or "").lower()
    if plan == "grandfather":
        return True

    status = (user.subscription_status or "").lower()
    if status != "pro_active":
        return False

    expires = user.plan_expires_at
    if expires is None:
        return False

    return expires > datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────
# Лимиты
# ─────────────────────────────────────────────────────────────
async def count_last_24h(
    session: AsyncSession,
    user_id: int,
    kind: LimitKind,
) -> int:
    """Считает использованные операции за последние 24 часа."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ROLLING_WINDOW_HOURS)

    if kind == "vacancies":
        stmt = (
            select(func.count(VacancyMatch.id))
            .where(VacancyMatch.user_id == user_id)
            .where(VacancyMatch.delivered_at >= cutoff)
        )
    elif kind == "breakdown":
        stmt = (
            select(func.count(ScoreBreakdown.id))
            .where(ScoreBreakdown.user_id == user_id)
            .where(ScoreBreakdown.created_at >= cutoff)
        )
    elif kind == "resume":
        stmt = (
            select(func.count(ResumeUsage.id))
            .where(ResumeUsage.user_id == user_id)
            .where(ResumeUsage.generated_at >= cutoff)
        )
    elif kind == "cover_letter":
        stmt = (
            select(func.count(CoverLetterUsage.id))
            .where(CoverLetterUsage.user_id == user_id)
            .where(CoverLetterUsage.generated_at >= cutoff)
        )
    else:
        raise ValueError(f"Unknown limit kind: {kind}")

    result = await session.execute(stmt)
    return result.scalar() or 0


async def check_daily_limit(
    session: AsyncSession,
    user_id: int,
    kind: LimitKind,
) -> tuple[bool, int, int]:
    """Проверяет лимит по типу операции за последние 24 часа.

    Возвращает (allowed, used, limit):
    - allowed: True если ещё можно (used < limit)
    - used: сколько уже потрачено
    - limit: максимум
    """
    limit = DAILY_LIMITS[kind]
    used = await count_last_24h(session, user_id, kind)
    return used < limit, used, limit


def remaining_today(used: int, kind: LimitKind) -> int:
    """Сколько осталось на сегодня."""
    return max(0, DAILY_LIMITS[kind] - used)
