"""Automatic provisioning of default sources for users."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Source, SourceType
from src.sources.career_sites import get_career_site_ids
from src.sources.channels_config import TELEGRAM_CHANNELS


async def provision_default_sources(session: AsyncSession, user_id: int) -> int:
    """Make sure user has all configured sources. Idempotent."""
    result = await session.execute(
        select(Source.source_type, Source.identifier).where(Source.user_id == user_id)
    )
    existing = {(row[0], row[1]) for row in result}

    created = 0

    # Telegram channels
    for channel in TELEGRAM_CHANNELS:
        key = (SourceType.telegram_channel, channel)
        if key in existing:
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

    # Career sites
    for site_id in get_career_site_ids():
        key = (SourceType.career_site, site_id)
        if key in existing:
            continue
        session.add(
            Source(
                user_id=user_id,
                source_type=SourceType.career_site,
                identifier=site_id,
                is_active=True,
            )
        )
        created += 1

    await session.flush()
    return created