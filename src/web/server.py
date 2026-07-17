"""HTTP server для приёма webhook'ов от Tribute Subscriptions.

Запускается параллельно с polling бота, слушает на /webhooks/tribute и /health.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from aiogram import Bot
from aiohttp import web
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings
from src.db.models import TributeWebhookEvent
from src.db.session import engine
from src.services.billing import (
    handle_cancelled_subscription,
    handle_new_subscription,
    handle_renewed_subscription,
)
from src.services.tribute import get_tribute_client

log = structlog.get_logger(__name__)


# Маппинг event name -> обработчик
EVENT_HANDLERS = {
    "new_subscription": handle_new_subscription,
    "renewed_subscription": handle_renewed_subscription,
    "cancelled_subscription": handle_cancelled_subscription,
}


# Сообщения юзерам по событиям Tribute
# Различаем weekly / monthly по payload["period"]
USER_MESSAGES_WEEKLY = {
    "new_subscription": (
        "Подписка оформлена, Pro 💎 активен на неделю.\n\n"
        "Теперь я буду присылать тебе несколько подборок в день, до 5 вакансий за раз, "
        "и писать до 5 сопроводительных в день.\n\n"
        "Следующее списание — {expires:%d.%m.%Y} (349₽), предупрежу заранее.\n\n"
        "Управлять подпиской: /my_plan\n"
        "Отменить: /cancel_subscription"
    ),
    "renewed_subscription": (
        "Подписка продлена на неделю, Pro 💎 активен до {expires:%d.%m.%Y}.\n\n"
        "Следующее списание — {expires:%d.%m.%Y} (349₽)."
    ),
}

USER_MESSAGES_MONTHLY = {
    "new_subscription": (
        "Подписка оформлена, Pro 💎 активен на месяц.\n\n"
        "Теперь я буду присылать тебе несколько подборок в день, до 5 вакансий за раз, "
        "и писать до 5 сопроводительных в день.\n\n"
        "Следующее списание — {expires:%d.%m.%Y} (990₽), предупрежу заранее.\n\n"
        "Управлять подпиской: /my_plan\n"
        "Отменить: /cancel_subscription"
    ),
    "renewed_subscription": (
        "Подписка продлена на месяц, Pro 💎 активен до {expires:%d.%m.%Y}.\n\n"
        "Следующее списание — {expires:%d.%m.%Y} (990₽)."
    ),
}

# Общие сообщения независимые от периода
USER_MESSAGES_COMMON = {
    "cancelled_subscription": (
        "Подписка отменена. Pro будет работать до {expires:%d.%m.%Y}, дальше — Free.\n\n"
        "Если передумаешь — /upgrade всегда на месте."
    ),
}



async def _record_event(
    session: AsyncSession,
    event_name: str,
    order_uuid: Optional[str],
    sent_at: datetime,
    payload: dict,
    signature_valid: bool,
) -> bool:
    """Записать event в БД с idempotency.

    Возвращает True если запись новая, False если дубль (Tribute ретраит до 24ч).
    """
    stmt = (
        insert(TributeWebhookEvent)
        .values(
            event_name=event_name,
            order_uuid=order_uuid,
            sent_at=sent_at,
            payload=payload,
            signature_valid=signature_valid,
        )
        .on_conflict_do_nothing(constraint="uq_tribute_event")
        .returning(TributeWebhookEvent.id)
    )
    result = await session.execute(stmt)
    row = result.first()
    await session.commit()
    return row is not None


def _parse_envelope(envelope: dict) -> tuple[str, dict, datetime]:
    """Разобрать обёртку webhook'а в (event_name, payload, sent_at).

    Формат Tribute Subscriptions:
    { "name": "new_subscription", "created_at": "...", "sent_at": "...", "payload": {...} }
    """
    event_name = envelope.get("name") or envelope.get("event") or "unknown"
    payload = envelope.get("payload") or envelope
    sent_at_raw = envelope.get("sent_at") or envelope.get("created_at")

    try:
        if sent_at_raw:
            sent_at = datetime.fromisoformat(sent_at_raw.replace("Z", "+00:00"))
        else:
            sent_at = datetime.now(timezone.utc)
    except (AttributeError, ValueError):
        sent_at = datetime.now(timezone.utc)

    return event_name, payload, sent_at


async def tribute_webhook(request: web.Request) -> web.Response:
    """Главный handler webhook'ов Tribute."""
    raw_body = await request.read()
    signature = request.headers.get("trbt-signature", "")

    # 1. Проверка подписи
    try:
        client = get_tribute_client()
    except Exception:
        log.exception("tribute_client_not_configured")
        return web.Response(status=503, text="tribute not configured")

    is_valid = client.verify_signature(raw_body, signature)
    if not is_valid:
        log.warning("tribute_invalid_signature", signature=signature[:16])
        return web.Response(status=401, text="invalid signature")

    # 2. Парсинг JSON
    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        log.warning("tribute_invalid_json")
        return web.Response(status=400, text="invalid json")

    event_name, payload, sent_at = _parse_envelope(envelope)

    # В subscriptions webhook'ах уникальный id — subscription_id (integer).
    # Используем его как order_uuid для идемпотентности (поле VARCHAR — влезет строка).
    subscription_id = payload.get("subscription_id") if isinstance(payload, dict) else None
    event_key = str(subscription_id) if subscription_id else None

    session_factory: async_sessionmaker[AsyncSession] = request.app["session_factory"]
    bot: Bot = request.app["bot"]

    # 3. Idempotency + диспетчер
    async with session_factory() as session:
        is_new = await _record_event(
            session, event_name, event_key, sent_at, payload, is_valid
        )
        if not is_new:
            log.info("tribute_event_duplicate", event_type=event_name, subscription_id=subscription_id)
            return web.Response(status=200, text="duplicate")

        log.info("tribute_event_received", event_type=event_name, subscription_id=subscription_id)

        handler = EVENT_HANDLERS.get(event_name)
        if not handler:
            log.warning("tribute_event_unknown", event_type=event_name, payload=payload)
            return web.Response(status=200, text="unknown event ignored")

        try:
            telegram_id = await handler(session, payload)
        except Exception:
            log.exception("tribute_handler_error", event_type=event_name)
            # 200 чтобы Tribute не ретраил 24 часа — ошибка уже в логах
            return web.Response(status=200, text="handler error, swallowed")

    # 4. Уведомить юзера
    if telegram_id:
        await _notify_user(bot, event_name, telegram_id, payload)

    return web.Response(status=200, text="ok")


async def _notify_user(
    bot: Bot,
    event_name: str,
    telegram_id: int,
    payload: dict,
) -> None:
    """Послать юзеру сообщение по итогам обработки события."""
    try:
        # Общие события (независимо от периода)
        template = USER_MESSAGES_COMMON.get(event_name)

        # События, зависящие от периода
        if not template:
            period = payload.get("period", "monthly")
            if period == "weekly":
                template = USER_MESSAGES_WEEKLY.get(event_name)
            else:
                template = USER_MESSAGES_MONTHLY.get(event_name)

        if not template:
            return

        # Парсим expires_at или считаем от now
        expires = None
        expires_raw = payload.get("expires_at")
        if expires_raw:
            try:
                expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            except ValueError:
                expires = None
        if not expires:
            days = 7 if payload.get("period") == "weekly" else 30
            expires = datetime.now(timezone.utc) + timedelta(days=days)

        text = template.format(expires=expires)
        await bot.send_message(telegram_id, text)
    except Exception:
        log.exception("notify_user_failed", telegram_id=telegram_id)


async def health(request: web.Request) -> web.Response:
    """Health check для Railway."""
    return web.Response(status=200, text="ok")


def create_web_app(bot: Bot) -> web.Application:
    """Создать aiohttp приложение с webhook handler'ом и health check."""
    app = web.Application()
    app["bot"] = bot
    app["session_factory"] = async_sessionmaker(engine, expire_on_commit=False)

    app.router.add_post(settings.tribute_webhook_path, tribute_webhook)
    app.router.add_get("/health", health)
    return app
