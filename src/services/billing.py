"""Billing service: обработка событий подписок Tribute и состояния платного плана.

Работает с публичным Tribute Subscriptions API (не Shop API).
События: new_subscription / renewed_subscription / cancelled_subscription.

Не путать с `subscription.py` — там проверка подписки на Telegram-канал (gate Free-плана).
Здесь — про деньги и Pro-тариф.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import User

log = structlog.get_logger(__name__)


# Период оплаты -> количество дней доступа Pro (для расчёта fallback expiry)
PERIOD_DAYS = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "halfyearly": 180,
    "yearly": 365,
}


# --- Helpers ---


async def _get_user_by_telegram_id(
    session: AsyncSession,
    telegram_id: Optional[int],
) -> Optional[User]:
    """В Tribute subscriptions webhook telegram_user_id приходит прямо в payload."""
    if not telegram_id:
        return None
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def _get_user_by_subscription_id(
    session: AsyncSession,
    subscription_id: Optional[int],
) -> Optional[User]:
    """Fallback на случай если в webhook telegram_user_id не пришёл."""
    if not subscription_id:
        return None
    result = await session.execute(
        select(User).where(User.tribute_subscription_id == subscription_id)
    )
    return result.scalar_one_or_none()


async def _resolve_user(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[User]:
    """Сначала пробуем по telegram_user_id, потом по subscription_id (fallback)."""
    user = await _get_user_by_telegram_id(session, payload.get("telegram_user_id"))
    if user:
        return user
    return await _get_user_by_subscription_id(session, payload.get("subscription_id"))


def _parse_expires_at(payload: dict[str, Any]) -> datetime:
    """Достаём expires_at из Tribute payload, fallback — считаем от now по period."""
    expires_raw = payload.get("expires_at")
    if expires_raw:
        try:
            return datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    days = PERIOD_DAYS.get(payload.get("period", "monthly"), 30)
    return datetime.now(timezone.utc) + timedelta(days=days)


# --- Event handlers ---


async def handle_new_subscription(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """new_subscription — первая оплата, Pro активирован.

    Возвращает telegram_id для последующего уведомления, или None.
    """
    user = await _resolve_user(session, payload)
    if not user:
        log.warning("new_subscription_no_user", payload=payload)
        return None

    user.plan = "pro"
    user.subscription_status = "pro_active"
    user.tribute_subscription_id = payload.get("subscription_id")
    user.plan_expires_at = _parse_expires_at(payload)
    user.last_payment_at = datetime.now(timezone.utc)
    user.auto_renew = True
    user.expiry_reminder_sent_at = None
    await session.commit()
    log.info(
        "new_subscription_pro_activated",
        user_id=user.id,
        subscription_id=payload.get("subscription_id"),
        expires=user.plan_expires_at.isoformat() if user.plan_expires_at else None,
    )
    return user.telegram_id


async def handle_renewed_subscription(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """renewed_subscription — рекуррентное списание прошло."""
    user = await _resolve_user(session, payload)
    if not user:
        log.warning("renewed_subscription_no_user", payload=payload)
        return None

    user.plan = "pro"
    user.subscription_status = "pro_active"
    user.plan_expires_at = _parse_expires_at(payload)
    user.last_payment_at = datetime.now(timezone.utc)
    user.auto_renew = True
    user.expiry_reminder_sent_at = None
    await session.commit()
    log.info(
        "renewed_subscription_pro_extended",
        user_id=user.id,
        expires=user.plan_expires_at.isoformat() if user.plan_expires_at else None,
    )
    return user.telegram_id


async def handle_cancelled_subscription(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """cancelled_subscription — юзер отменил.

    Доступ к Pro сохраняется до expires_at, потом cron сделает downgrade.
    """
    user = await _resolve_user(session, payload)
    if not user:
        log.warning("cancelled_subscription_no_user", payload=payload)
        return None

    expires = _parse_expires_at(payload)
    user.subscription_status = "pro_cancelled_until_expiry"
    user.plan_expires_at = expires
    user.auto_renew = False
    await session.commit()
    log.info(
        "subscription_cancelled",
        user_id=user.id,
        reason=payload.get("cancel_reason", ""),
        expires=expires.isoformat(),
    )
    return user.telegram_id


# --- Cron / scheduled jobs ---


async def downgrade_expired_subscriptions(session: AsyncSession) -> int:
    """Раз в час: помечает expired у юзеров с истёкшим plan_expires_at.

    Основной механизм downgrade для отменённых подписок (после cancel).
    Также страхует активные подписки от потерянных webhook'ов.
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(User).where(
            User.plan_expires_at.is_not(None),
            User.plan_expires_at < now,
            User.subscription_status.in_(
                ["pro_active", "pro_cancelled_until_expiry"]
            ),
        )
    )
    count = 0
    for user in result.scalars():
        user.plan = "free"
        user.subscription_status = "pro_expired"
        user.auto_renew = False
        count += 1
    if count:
        await session.commit()
        log.info("subscriptions_expired_downgraded", count=count)
    return count