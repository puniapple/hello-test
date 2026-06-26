"""HTTP server для приёма webhook'ов от Tribute.

Запускается параллельно с polling бота, слушает на /webhooks/tribute и /health.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from aiogram import Bot
from aiohttp import web
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings
from src.db.models import TributeWebhookEvent
from src.db.session import engine
from src.services.billing import (
    handle_cancelled,
    handle_charge_failed,
    handle_charge_success,
    handle_payment_failed,
    handle_payment_received,
    handle_refunded,
    is_recurring_payload,
)
from src.services.tribute import get_tribute_client

log = structlog.get_logger(__name__)


# Маппинг event name -> обработчик
EVENT_HANDLERS = {
    "shopOrderPaymentReceived": handle_payment_received,
    "shopOrder": handle_payment_received,  # на случай если общий event тоже шлётся
    "shopOrderChargeSuccess": handle_charge_success,
    "shopOrderChargeFailed": handle_charge_failed,
    "shopOrderPaymentFailed": handle_payment_failed,
    "shopOrderCancelled": handle_cancelled,
    "shopOrderRefunded": handle_refunded,
}


# Сообщения для подписочных юзеров (с автопродлением)
USER_MESSAGES_RECURRING = {
    "payment_received": (
        "✨ Оплата получена. Pro подписка активирована до {expires:%d.%m.%Y}.\n\n"
        "Теперь матчинг идёт три раза в день, до 8 вакансий за цикл. "
        "Дальше карта будет продлеваться сама — отменить можно в любой момент через /cancel_subscription."
    ),
    "charge_success": (
        "💎 Подписка продлена ещё на месяц. Pro до {expires:%d.%m.%Y}.\n"
        "Спасибо что остаёшься со мной."
    ),
    "charge_failed": (
        "⚠️ Не получилось списать оплату с карты.\n"
        "Tribute попробует ещё пару раз в течение ближайших часов. "
        "Если хочешь — обнови способ оплаты в @tribute."
    ),
    "payment_failed": (
        "⚠️ Платёж не прошёл. Попробуй ещё раз через /upgrade."
    ),
    "cancelled": (
        "Подписка отменена. Доступ к Pro останется до {expires:%d.%m.%Y}.\n"
        "Возвращайся когда захочешь — /upgrade всё ещё ждёт."
    ),
    "refunded": (
        "Возврат оформлен. Pro отключён, бот вернулся в бесплатный режим."
    ),
}


# Сообщения для разовых юзеров (без автопродления)
USER_MESSAGES_ONETIME = {
    "payment_received": (
        "✨ Оплата получена. Pro активирован до {expires:%d.%m.%Y}.\n\n"
        "Теперь матчинг идёт три раза в день, до 8 вакансий за цикл. "
        "Это разовый платёж — карта не привязана, по истечении 30 дней нужно будет купить снова."
    ),
    "payment_failed": (
        "⚠️ Платёж не прошёл. Попробуй ещё раз через /upgrade."
    ),
    "refunded": (
        "Возврат оформлен. Pro отключён, бот вернулся в бесплатный режим."
    ),
}


EVENT_TO_MESSAGE_KEY = {
    "shopOrderPaymentReceived": "payment_received",
    "shopOrder": "payment_received",
    "shopOrderChargeSuccess": "charge_success",
    "shopOrderChargeFailed": "charge_failed",
    "shopOrderPaymentFailed": "payment_failed",
    "shopOrderCancelled": "cancelled",
    "shopOrderRefunded": "refunded",
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

    Возвращает True если запись новая, False если дубль (тогда обрабатывать не нужно).
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

    Формат envelope ожидаем такой (по аналогии с Digital Products API):
    { "name": "...", "created_at": "...", "sent_at": "...", "payload": {...} }

    Если Shop API использует другой формат — первый реальный webhook покажет в логах.
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
    order_uuid = payload.get("uuid") if isinstance(payload, dict) else None

    session_factory: async_sessionmaker[AsyncSession] = request.app["session_factory"]
    bot: Bot = request.app["bot"]

    # 3. Idempotency + диспетчер
    async with session_factory() as session:
        is_new = await _record_event(
            session, event_name, order_uuid, sent_at, payload, is_valid
        )
        if not is_new:
            log.info("tribute_event_duplicate", event=event_name, uuid=order_uuid)
            return web.Response(status=200, text="duplicate")

        log.info("tribute_event_received", event=event_name, uuid=order_uuid)

        handler = EVENT_HANDLERS.get(event_name)
        if not handler:
            log.warning("tribute_event_unknown", event=event_name, payload=payload)
            return web.Response(status=200, text="unknown event ignored")

        try:
            telegram_id = await handler(session, payload)
        except Exception:
            log.exception("tribute_handler_error", event=event_name)
            # Возвращаем 200 чтобы Tribute не ретраил 24 часа — ошибка уже в логах
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
        message_key = EVENT_TO_MESSAGE_KEY.get(event_name)
        if not message_key:
            return

        # Выбираем набор сообщений в зависимости от типа подписки
        recurring = is_recurring_payload(payload)
        messages = USER_MESSAGES_RECURRING if recurring else USER_MESSAGES_ONETIME

        template = messages.get(message_key)
        if not template:
            return  # для разовой нет смысла слать события про продление/отмену

        # Парсим memberExpiresAt или считаем по периоду
        expires = None
        expires_raw = payload.get("memberExpiresAt")
        if expires_raw:
            try:
                expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            except ValueError:
                expires = None
        if not expires:
            expires = datetime.now(timezone.utc) + timedelta(days=30)

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