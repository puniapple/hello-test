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


async def downgrade_expired_subscriptions(session: AsyncSession, bot=None) -> dict:
    """Раз в час: помечает expired у юзеров с истёкшим plan_expires_at.
    
    Различает два сценария:
    - 5.11: subscription_status='pro_cancelled_until_expiry' → юзер сам отменил, срок вышел
    - 5.8: subscription_status='pro_active' → renewal не прошёл (Tribute не смог списать)
    
    Возвращает dict со счётчиками по типам событий.
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
    
    cancelled_expired = 0  # 5.11
    renewal_failed = 0  # 5.8
    users_to_notify = []
    
    for user in result.scalars():
        was_cancelled = user.subscription_status == "pro_cancelled_until_expiry"
        user.plan = "free"
        user.subscription_status = "pro_expired"
        user.auto_renew = False
        
        if was_cancelled:
            cancelled_expired += 1
            users_to_notify.append((user.telegram_id, "cancelled_expired"))
        else:
            renewal_failed += 1
            users_to_notify.append((user.telegram_id, "renewal_failed"))
    
    if cancelled_expired or renewal_failed:
        await session.commit()
        log.info(
            "subscriptions_expired_downgraded",
            cancelled_expired=cancelled_expired,
            renewal_failed=renewal_failed,
        )
    
    # Уведомляем юзеров
    if bot:
        for telegram_id, reason in users_to_notify:
            try:
                if reason == "cancelled_expired":
                    text = (
                        "Pro закончился, теперь ты на Free.\n\n"
                        "— 1 подборка в день\n"
                        "— До 3 вакансий за раз\n"
                        "— 1 сопроводительное в день\n\n"
                        "Вернуть Pro: /upgrade"
                    )
                else:  # renewal_failed
                    text = (
                        "Не получилось продлить Pro.\n\n"
                        "Ты вернулся на Free: 1 подборка в день, до 3 вакансий, "
                        "1 сопроводительное в день.\n\n"
                        "Когда будешь готов — оплати заново через /upgrade. "
                        "Профиль и история сохранены."
                    )
                await bot.send_message(telegram_id, text)
            except Exception:
                log.exception("expire_notify_failed", telegram_id=telegram_id)
    
    return {
        "cancelled_expired": cancelled_expired,
        "renewal_failed": renewal_failed,
    }


async def send_renewal_reminders(session: AsyncSession, bot=None) -> dict:
    """Раз в день: напоминания перед списанием.
    
    - 5.4: за 7 дней до списания (только monthly-подписки)
    - 5.5: за 1 день до списания (все активные подписки)
    """
    now = datetime.now(timezone.utc)
    if not bot:
        return {"reminded_7d": 0, "reminded_1d": 0}
    
    # 5.4 — за 7 дней (only monthly, weekly не шлём: у них период всего 7 дней)
    seven_days_start = now + timedelta(days=7)
    seven_days_end = now + timedelta(days=8)
    result_7d = await session.execute(
        select(User).where(
            User.subscription_status == "pro_active",
            User.plan_expires_at.between(seven_days_start, seven_days_end),
            User.auto_renew.is_(True),
        )
    )
    reminded_7d = 0
    for user in result_7d.scalars():
        # Определяем monthly по разнице (expires - last_payment)
        # Если > 20 дней — это monthly. Если < 20 — weekly, пропускаем
        if user.last_payment_at:
            period_days = (user.plan_expires_at - user.last_payment_at).days
            if period_days < 20:
                continue  # weekly
        
        try:
            text = (
                f"Через 7 дней — {user.plan_expires_at.strftime('%d.%m.%Y')} — "
                f"спишется 990₽ за следующий месяц Pro.\n\n"
                f"Если хочешь отменить заранее — /cancel_subscription. Никаких автосписаний без твоего ведома.\n\n"
                f"Всё ок — ничего делать не надо."
            )
            await bot.send_message(user.telegram_id, text)
            reminded_7d += 1
        except Exception:
            log.exception("reminder_7d_failed", telegram_id=user.telegram_id)
    
    # 5.5 — за 1 день (weekly + monthly)
    one_day_start = now + timedelta(days=1)
    one_day_end = now + timedelta(days=2)
    result_1d = await session.execute(
        select(User).where(
            User.subscription_status == "pro_active",
            User.plan_expires_at.between(one_day_start, one_day_end),
            User.auto_renew.is_(True),
        )
    )
    reminded_1d = 0
    for user in result_1d.scalars():
        # Определяем weekly/monthly для правильной суммы
        is_weekly = True
        if user.last_payment_at:
            period_days = (user.plan_expires_at - user.last_payment_at).days
            is_weekly = period_days < 20
        
        amount = "349₽" if is_weekly else "990₽"
        period_word = "неделю" if is_weekly else "месяц"
        
        try:
            text = (
                f"Завтра — {user.plan_expires_at.strftime('%d.%m.%Y')} — "
                f"спишется {amount} за следующую {period_word} Pro.\n\n"
                f"Всё ок — ничего делать не надо.\n\n"
                f"Если хочешь отменить — /cancel_subscription до завтрашнего утра."
            )
            await bot.send_message(user.telegram_id, text)
            reminded_1d += 1
        except Exception:
            log.exception("reminder_1d_failed", telegram_id=user.telegram_id)
    
    if reminded_7d or reminded_1d:
        log.info("renewal_reminders_sent", reminded_7d=reminded_7d, reminded_1d=reminded_1d)
    
    return {"reminded_7d": reminded_7d, "reminded_1d": reminded_1d}