"""Internal service for managing user sources and dedup."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SeenVacancy, Source, SourceType
from src.sources.base import Vacancy


async def add_source(
    session: AsyncSession,
    user_id: int,
    source_type: SourceType,
    identifier: str,
    filters: dict | None = None,
) -> Source:
    src = Source(
        user_id=user_id,
        source_type=source_type,
        identifier=identifier,
        filters=filters,
        is_active=True,
    )
    session.add(src)
    await session.flush()
    return src


async def list_user_sources(
    session: AsyncSession,
    user_id: int,
    source_type: SourceType | None = None,
) -> list[Source]:
    stmt = select(Source).where(Source.user_id == user_id, Source.is_active.is_(True))
    if source_type is not None:
        stmt = stmt.where(Source.source_type == source_type)
    result = await session.execute(stmt.order_by(Source.id))
    return list(result.scalars())


async def deactivate_sources(
    session: AsyncSession,
    user_id: int,
    source_type: SourceType,
) -> int:
    """Soft-delete: mark all sources of given type inactive. Returns count."""
    sources = await list_user_sources(session, user_id, source_type)
    for s in sources:
        s.is_active = False
    return len(sources)


async def filter_unseen(
    session: AsyncSession,
    user_id: int,
    vacancies: list[Vacancy],
) -> list[Vacancy]:
    if not vacancies:
        return []
    hashes = [v.hash for v in vacancies]
    result = await session.execute(
        select(SeenVacancy.vacancy_hash).where(
            SeenVacancy.user_id == user_id,
            SeenVacancy.vacancy_hash.in_(hashes),
        )
    )
    seen = {row[0] for row in result}
    return [v for v in vacancies if v.hash not in seen]


async def mark_seen(
    session: AsyncSession,
    user_id: int,
    vacancies: list[Vacancy],
) -> None:
    for v in vacancies:
        session.add(
            SeenVacancy(user_id=user_id, vacancy_hash=v.hash, source_type=v.source_type)
        )