"""Automatic provisioning of default sources for new users."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Source, SourceType
from src.sources.channels_config import TELEGRAM_CHANNELS


async def provision_default_sources(session: AsyncSession, user_id: int) -> int:
    """Make sure user has all configured Telegram channels as sources.

    Idempotent: existing sources are not duplicated. Returns count of newly created.
    """
    result = await session.execute(
        select(Source.identifier).where(
            Source.user_id == user_id,
            Source.source_type == SourceType.telegram_channel,
        )
    )
    existing = {row[0] for row in result}

    created = 0
    for channel in TELEGRAM_CHANNELS:
        if channel in existing:
            continue
        session.add(
            Source(
                user_id=user_id,
                source_type=SourceType.telegram_channel,
                identifier=channel,
                is_active=True,
            )
        )
        created += 1

    await session.flush()
    return created