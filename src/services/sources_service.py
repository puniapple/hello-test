"""Internal service for managing user sources and dedup.

NOTE (unified pool model, июль 2026):
- Все юзеры получают единый пул источников из реестра кода
  (career_sites._registry + константа DEFAULT_TELEGRAM_CHANNELS ниже).
- Таблица Source в БД остаётся, но новая логика цикла её не читает и не пишет.
- Матчер + pre-filter отвечают за то, чтобы юзер получал только релевантное.
"""

from __future__ import annotations

from sqlalchemy import select, false, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SeenVacancy, Source, SourceType
from src.sources.base import Vacancy
from src.sources.career_sites import get_career_site_ids


# Единый список Telegram-каналов, которые фетчатся всем юзерам.
# Добавила новый канал — вписала сюда, при следующем цикле пойдёт всем.
DEFAULT_TELEGRAM_CHANNELS: list[str] = [
    "alfabank_career",
    "analyst_jobs",
    "b2bsalesjobs",
    "bezaspera",
    "budujobs",
    "careerspace",
    "choicy_work",
    "cmo_jobs",
    "communication_department",
    "cxhr_jobs",
    "data_jobs_ru",
    "dddwork",
    "devjobs_ru",
    "digitalstrategy_jobs",
    "doctorjobs",
    "edtech_jobs_ru",
    "edujobs",
    "femtechforce",
    "forproducts",
    "growth_jobs",
    "hcareers_jobs",
    "hireproproduct",
    "huggabletalents",
    "it_vakansii_jobs",
    "itjobs_ru",
    "javajobs",
    "kommersant_career",
    "marketing_jobs_ru",
    "marketingjobs",
    "medjobs_ru",
    "medtech_career",
    "mirkreatorovjob",
    "netology_career",
    "normrabota",
    "practicum_experts",
    "product_jobs",
    "product_market_fit_jobs",
    "projects_jobs_feed",
    "remocate",
    "remote_jobs_relocate",
    "rusukrjobs",
    "salesjobs_ru",
    "skillbox_career",
    "spb_dev_jobs",
    "theblueprintcareer",
    "theyseeku",
    "uxjobs",
    "vitrinajobs",
    "vkjobs",
    "whiteedtechwork",
    "zarubezhom_jobs",
    "zdemcv",
]


def _build_sources_from_registry() -> list[Source]:
    """Собираем in-memory Source-объекты из реестра кода.

    Возвращает не-persistent (не привязанные к БД) ORM-объекты Source
    с заполненными identifier / source_type / is_active. Их достаточно
    для _fetch_from_all_sources — он читает только эти поля.
    """
    sources: list[Source] = []

    for site_id in get_career_site_ids():
        sources.append(
            Source(
                user_id=0,  # sentinel, не используется дальше по цепочке
                source_type=SourceType.career_site,
                identifier=site_id,
                is_active=True,
                filters=None,
            )
        )

    for channel in DEFAULT_TELEGRAM_CHANNELS:
        sources.append(
            Source(
                user_id=0,
                source_type=SourceType.telegram_channel,
                identifier=channel,
                is_active=True,
                filters=None,
            )
        )

    return sources


# Кешируем на всё время процесса — реестр в коде не меняется без рестарта.
_CACHED_SOURCES: list[Source] | None = None


async def add_source(
    session: AsyncSession,
    user_id: int,
    source_type: SourceType,
    identifier: str,
    filters: dict | None = None,
) -> Source:
    """Оставлено для обратной совместимости со старыми скриптами.

    Записывает в БД, но новая логика цикла эти записи не читает.
    """
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
    """Возвращает единый пул источников из реестра.

    Аргументы session и user_id сохранены в сигнатуре, но игнорируются —
    все юзеры получают одинаковый набор. Фильтр по source_type работает,
    если явно передан.
    """
    global _CACHED_SOURCES
    if _CACHED_SOURCES is None:
        _CACHED_SOURCES = _build_sources_from_registry()

    if source_type is None:
        return list(_CACHED_SOURCES)
    return [s for s in _CACHED_SOURCES if s.source_type == source_type]


async def deactivate_sources(
    session: AsyncSession,
    user_id: int,
    source_type: SourceType,
) -> int:
    """No-op. Источники не привязаны к юзеру, отключать нечего.

    Оставлено, чтобы не падал существующий код, который мог это вызывать.
    """
    return 0


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