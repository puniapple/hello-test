"""Main job-search cycle: fetch -> dedup -> match -> send."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import structlog
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.matcher import MatchResult, VacancyMatcher
from src.bot.vacancy_message import build_reaction_keyboard, format_vacancy_message
from src.config import settings
from src.db.models import Profile, Source, SourceType, User, UserState, VacancyMatch
from src.db.session import async_session
from src.services.sources_service import filter_unseen, list_user_sources, mark_seen
from src.sources.base import JobSource, Vacancy
from src.sources.career_sites import CareerSiteSource
from src.sources.telegram_channel import TelegramChannelSource

logger = structlog.get_logger(__name__)

MAX_VACANCIES_PER_USER_PER_CYCLE = 150
MAX_DELIVERIES_PER_USER_PER_CYCLE = 8
USER_CONCURRENCY = 3


async def run_job_search_cycle(bot: Bot) -> dict:
    """One full pass: all active users, all their sources, match, deliver."""
    log = logger.bind(cycle_started_at=datetime.now(timezone.utc).isoformat())
    log.info("cycle_start")

    async with async_session() as session:
        result = await session.execute(
            select(User).where(
                User.is_active.is_(True),
                User.profile_ready_for_search.is_(True),
            )
        )
        users = list(result.scalars())

    log.info("active_users", count=len(users))
    if not users:
        return {"users": 0, "delivered": 0}

    semaphore = asyncio.Semaphore(USER_CONCURRENCY)
    stats = {"users": 0, "delivered": 0, "matched_total": 0, "fetched_total": 0}

    async def process_one(user: User) -> dict:
        async with semaphore:
            return await _process_user(bot, user)

    results = await asyncio.gather(
        *(process_one(u) for u in users), return_exceptions=True
    )

    for u, r in zip(users, results):
        if isinstance(r, Exception):
            log.error("user_failed", user_id=u.id, error=str(r))
            continue
        stats["users"] += 1
        stats["delivered"] += r.get("delivered", 0)
        stats["matched_total"] += r.get("matched", 0)
        stats["fetched_total"] += r.get("fetched", 0)

    log.info("cycle_done", **stats)
    return stats


async def _process_user(bot: Bot, user: User) -> dict:
    """Full pipeline for one user."""
    log = logger.bind(user_id=user.id, telegram_id=user.telegram_id)

    # Subscription gate: если канал настроен и юзер отписался — пропускаем
    from src.services.subscription import is_required_channel_configured, is_subscribed
    if is_required_channel_configured():
        subscribed = await is_subscribed(bot, user.telegram_id)
        if not subscribed:
            log.info("skip_not_subscribed")
            return {"fetched": 0, "matched": 0, "delivered": 0}

    async with async_session() as session:
        # 1. Load profile
        profile_result = await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )
        profile = profile_result.scalar_one_or_none()
        if profile is None or not profile.profile_data:
            log.info("skip_no_profile")
            return {"fetched": 0, "matched": 0, "delivered": 0}

        # 2. List active sources
        sources = await list_user_sources(session, user.id)
        if not sources:
            log.info("skip_no_sources")
            return {"fetched": 0, "matched": 0, "delivered": 0}

        # 3. Fetch from all sources
        all_fetched = await _fetch_from_all_sources(sources)
        log.info("fetched", count=len(all_fetched))

        # 4. Dedupe against history (and within the batch)
        fresh = await filter_unseen(session, user.id, all_fetched)
        log.info("fresh_after_dedup", count=len(fresh))

        if not fresh:
            return {"fetched": len(all_fetched), "matched": 0, "delivered": 0}

        # 5. Cap how many we'll match (cost control), shuffle first для честной выборки
        random.shuffle(fresh)
        to_match = fresh[:MAX_VACANCIES_PER_USER_PER_CYCLE]
        deferred = fresh[MAX_VACANCIES_PER_USER_PER_CYCLE:]
        log.info("matching", count=len(to_match), deferred=len(deferred))

        # 6. Mark only matched items as seen. Deferred ones stay unseen
        # so they can be picked up in subsequent cycles, not lost forever.
        await mark_seen(session, user.id, to_match)
        await session.commit()

        # 7. Match each
        matcher = VacancyMatcher()
        deliveries: list[tuple[Vacancy, MatchResult]] = []
        for vacancy in to_match:
            try:
                match = await matcher.match(profile.profile_data, vacancy)
            except Exception as e:
                log.warning("match_failed", url=vacancy.url, error=str(e))
                continue
            if match.should_send:
                deliveries.append((vacancy, match))

        # 8. Sort by score desc, cap to delivery limit
        deliveries.sort(key=lambda d: d[1].score, reverse=True)
        deliveries = deliveries[:MAX_DELIVERIES_PER_USER_PER_CYCLE]
        log.info("ready_to_deliver", count=len(deliveries))

        # 9. Persist matches and send to Telegram
        sent_count = 0
        for vacancy, match in deliveries:
            try:
                vm = VacancyMatch(
                    user_id=user.id,
                    vacancy_hash=vacancy.hash,
                    vacancy_data=vacancy.to_storage_dict(),
                    match_score=match.score,
                    match_reason=match.fit_reason,
                )
                session.add(vm)
                await session.flush()

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=format_vacancy_message(vacancy, match),
                    reply_markup=build_reaction_keyboard(vm.id),
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=False,
                )
                sent_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning("delivery_failed", url=vacancy.url, error=str(e))
                continue

        await session.commit()
        log.info("user_done", delivered=sent_count)
        return {"fetched": len(all_fetched), "matched": len(to_match), "delivered": sent_count}


async def _fetch_from_all_sources(sources: list[Source]) -> list[Vacancy]:
    """Fetch from every source in parallel, collect into one list.
    
    Каждый источник имеет жёсткий таймаут — если зависает, пропускаем.
    """
    tg = TelegramChannelSource()
    cs = CareerSiteSource()
    SOURCE_TIMEOUT = 30  # секунд на один источник

    async def fetch_one(s: Source) -> list[Vacancy]:
        try:
            if s.source_type == SourceType.telegram_channel:
                return await asyncio.wait_for(tg.fetch(s), timeout=SOURCE_TIMEOUT)
            if s.source_type == SourceType.career_site:
                return await asyncio.wait_for(cs.fetch(s), timeout=SOURCE_TIMEOUT)
            return []
        except asyncio.TimeoutError:
            logger.warning("source_timeout", identifier=s.identifier, timeout=SOURCE_TIMEOUT)
            return []
        except Exception as e:
            logger.warning("source_failed", identifier=s.identifier, error=str(e))
            return []

    chunks = await asyncio.gather(*(fetch_one(s) for s in sources))
    combined: list[Vacancy] = []
    for chunk in chunks:
        combined.extend(chunk)
    return combined