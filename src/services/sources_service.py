"""Internal service for managing user sources and dedup."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SeenVacancy, Source, SourceType
from src.sources.base import Vacancy
from sqlalchemy import false, or_


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
    """Drop vacancies already seen by this user (across three dedup keys).

    Checks:
      1. vacancy_hash (same source, same external_id) — точный дубликат
      2. content_fingerprint — репост с тем же текстом из другого канала
      3. global_external_id — одна вакансия со ссылкой на одну job-платформу
    """
    if not vacancies:
        return []

    hashes = [v.hash for v in vacancies]
    fingerprints = [v.content_fingerprint for v in vacancies]
    global_ids = [v.global_external_id for v in vacancies if v.global_external_id]

    result = await session.execute(
        select(
            SeenVacancy.vacancy_hash,
            SeenVacancy.content_fingerprint,
            SeenVacancy.global_external_id,
        ).where(
            SeenVacancy.user_id == user_id,
            or_(
                SeenVacancy.vacancy_hash.in_(hashes),
                SeenVacancy.content_fingerprint.in_(fingerprints),
                SeenVacancy.global_external_id.in_(global_ids) if global_ids else false(),
            ),
        )
    )
    seen_hashes: set[str] = set()
    seen_fingerprints: set[str] = set()
    seen_global: set[str] = set()
    for row in result:
        if row[0]:
            seen_hashes.add(row[0])
        if row[1]:
            seen_fingerprints.add(row[1])
        if row[2]:
            seen_global.add(row[2])

    # Также дедуплицируем внутри текущей пачки (если два канала прислали репост сейчас)
    fresh: list[Vacancy] = []
    batch_fingerprints: set[str] = set()
    batch_global: set[str] = set()
    for v in vacancies:
        if v.hash in seen_hashes:
            continue
        if v.content_fingerprint in seen_fingerprints or v.content_fingerprint in batch_fingerprints:
            continue
        if v.global_external_id and (
            v.global_external_id in seen_global or v.global_external_id in batch_global
        ):
            continue
        fresh.append(v)
        batch_fingerprints.add(v.content_fingerprint)
        if v.global_external_id:
            batch_global.add(v.global_external_id)
    return fresh


async def mark_seen(
    session: AsyncSession,
    user_id: int,
    vacancies: list[Vacancy],
) -> None:
    """Persist dedup keys for vacancies."""
    for v in vacancies:
        session.add(
            SeenVacancy(
                user_id=user_id,
                vacancy_hash=v.hash,
                source_type=v.source_type,
                content_fingerprint=v.content_fingerprint,
                global_external_id=v.global_external_id,
            )
        )