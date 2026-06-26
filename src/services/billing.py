"""Billing service: обработка событий Tribute и состояния платных подписок.

Не путать с `subscription.py` — там проверка подписки на Telegram-канал (gate Free-плана).
Здесь — про деньги, Pro и Tribute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import User

log = structlog.get_logger(__name__)


# Период оплаты -> количество дней доступа Pro
PERIOD_DAYS = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "halfyearly": 180,
    "yearly": 365,
    "onetime": 30,  # разовый Pro даёт 30 дней доступа
}

# Какие периоды считаются рекуррентными (автопродление включается)
RECURRING_PERIODS = {"weekly", "monthly", "quarterly", "halfyearly", "yearly"}


# --- Helpers ---


async def _get_user_by_customer_id(
    session: AsyncSession,
    customer_id: Optional[str],
) -> Optional[User]:
    """customerId, который мы передавали в create_order, == telegram_id юзера."""
    if not customer_id:
        return None
    try:
        telegram_id = int(customer_id)
    except (TypeError, ValueError):
        log.warning("invalid_customer_id", customer_id=customer_id)
        return None
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def _get_user_by_order_uuid(
    session: AsyncSession,
    order_uuid: Optional[str],
) -> Optional[User]:
    """Fallback на случай если в webhook не пришёл customerId."""
    if not order_uuid:
        return None
    result = await session.execute(
        select(User).where(User.tribute_order_uuid == order_uuid)
    )
    return result.scalar_one_or_none()


async def _resolve_user(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[User]:
    """Сначала пробуем по customerId, потом по order_uuid (fallback)."""
    user = await _get_user_by_customer_id(session, payload.get("customerId"))
    if user:
        return user
    return await _get_user_by_order_uuid(session, payload.get("uuid"))


def _compute_expiry(period: Optional[str], member_expires_at: Optional[str]) -> datetime:
    """Берём memberExpiresAt из Tribute если есть, иначе считаем от now."""
    if member_expires_at:
        try:
            return datetime.fromisoformat(member_expires_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    days = PERIOD_DAYS.get(period or "monthly", 30)
    return datetime.now(timezone.utc) + timedelta(days=days)


def is_recurring_payload(payload: dict[str, Any]) -> bool:
    """Подписка или разовая? Подписка == period в RECURRING_PERIODS или isRecurrent == True."""
    if payload.get("isRecurrent") is True:
        return True
    if payload.get("isRecurrent") is False:
        return False
    period = payload.get("period")
    return period in RECURRING_PERIODS


# --- Event handlers ---


async def handle_payment_received(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """shopOrderPaymentReceived — первичная оплата (карта/Stars/СБП).

    Работает и для подписки, и для разовой — различаем по period.
    Возвращает telegram_id юзера для последующего уведомления, или None.
    """
    user = await _resolve_user(session, payload)
    if not user:
        log.warning("payment_received_no_user", payload=payload)
        return None

    recurring = is_recurring_payload(payload)
    user.plan = "pro"
    user.subscription_status = "pro_active"
    user.tribute_order_uuid = payload.get("uuid")
    user.plan_expires_at = _compute_expiry(
        payload.get("period"), payload.get("memberExpiresAt")
    )
    user.last_payment_at = datetime.now(timezone.utc)
    user.auto_renew = recurring
    user.expiry_reminder_sent_at = None
    await session.commit()
    log.info(
        "payment_received_pro_activated",
        user_id=user.id,
        period=payload.get("period"),
        recurring=recurring,
        expires=user.plan_expires_at.isoformat() if user.plan_expires_at else None,
    )
    return user.telegram_id


async def handle_charge_success(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """shopOrderChargeSuccess — успешное рекуррентное списание (renewal)."""
    user = await _resolve_user(session, payload)
    if not user:
        log.warning("charge_success_no_user", payload=payload)
        return None

    user.plan = "pro"
    user.subscription_status = "pro_active"
    user.plan_expires_at = _compute_expiry(
        payload.get("period"), payload.get("memberExpiresAt")
    )
    user.last_payment_at = datetime.now(timezone.utc)
    user.auto_renew = True
    user.expiry_reminder_sent_at = None
    await session.commit()
    log.info(
        "charge_success_pro_extended",
        user_id=user.id,
        expires=user.plan_expires_at.isoformat() if user.plan_expires_at else None,
    )
    return user.telegram_id


async def handle_charge_failed(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """shopOrderChargeFailed — рекуррентное списание не прошло. 1-3 попытки, потом cancel."""
    user = await _resolve_user(session, payload)
    if not user:
        return None

    retries = payload.get("chargeRetries", 1)
    user.subscription_status = "pro_grace"
    await session.commit()
    log.info("charge_failed", user_id=user.id, retries=retries)
    return user.telegram_id


async def handle_payment_failed(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """shopOrderPaymentFailed — первичный платёж не прошёл."""
    user = await _resolve_user(session, payload)
    if not user:
        return None
    log.info(
        "payment_failed",
        user_id=user.id,
        error_code=payload.get("errorCode"),
        error_message=payload.get("errorMessage"),
    )
    return user.telegram_id


async def handle_cancelled(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """shopOrderCancelled — подписка отменена. Доступ до memberExpiresAt."""
    user = await _resolve_user(session, payload)
    if not user:
        return None

    reason = payload.get("cancelReason")
    expires = _compute_expiry(payload.get("period"), payload.get("memberExpiresAt"))

    user.subscription_status = "pro_cancelled_until_expiry"
    user.plan_expires_at = expires
    user.auto_renew = False
    await session.commit()
    log.info(
        "subscription_cancelled",
        user_id=user.id,
        reason=reason,
        expires=expires.isoformat(),
    )
    return user.telegram_id


async def handle_refunded(
    session: AsyncSession,
    payload: dict[str, Any],
) -> Optional[int]:
    """shopOrderRefunded — возврат. Сразу downgrade на free."""
    user = await _resolve_user(session, payload)
    if not user:
        return None

    user.plan = "free"
    user.subscription_status = "free"
    user.plan_expires_at = None
    user.auto_renew = False
    await session.commit()
    log.info("subscription_refunded", user_id=user.id)
    return user.telegram_id


# --- Cron / scheduled jobs ---


async def downgrade_expired_subscriptions(session: AsyncSession) -> int:
    """Раз в час: помечает expired у тех, у кого plan_expires_at прошёл.

    Страхует подписки от потерянных webhook'ов и downgrade'ит разовых юзеров
    (для разовых это единственный механизм downgrade).
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(User).where(
            User.plan_expires_at.is_not(None),
            User.plan_expires_at < now,
            User.subscription_status.in_(
                ["pro_active", "pro_grace", "pro_cancelled_until_expiry"]
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


async def find_onetime_users_to_remind(session: AsyncSession) -> list[User]:
    """Разовые Pro-юзеры, у которых до окончания 2-4 дня и не было напоминания."""
    now = datetime.now(timezone.utc)
    in_2_days = now + timedelta(days=2)
    in_4_days = now + timedelta(days=4)
    result = await session.execute(
        select(User).where(
            User.plan == "pro",
            User.auto_renew.is_(False),
            User.subscription_status == "pro_active",
            User.plan_expires_at.between(in_2_days, in_4_days),
            User.expiry_reminder_sent_at.is_(None),
        )
    )
    return list(result.scalars())


async def mark_reminder_sent(session: AsyncSession, user_id: int) -> None:
    """Пометить что напоминание о приближающемся окончании отправлено."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.expiry_reminder_sent_at = datetime.now(timezone.utc)
        await session.commit()