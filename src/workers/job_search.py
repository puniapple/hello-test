"""Main job-search cycle: fetch -> dedup -> match -> send."""

from __future__ import annotations

import asyncio
import logging
import random
import os
from datetime import datetime, timezone, timedelta

import structlog
from aiogram import Bot
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.matcher import MatchResult, VacancyMatcher
from src.bot.vacancy_message import build_reaction_keyboard, format_vacancy_message
from src.config import settings
from src.db.models import Profile, Source, SourceType, User, UserState, VacancyMatch
from src.db.session import async_session
from src.services.sources_service import filter_unseen, list_user_sources, mark_seen
from src.services.profile_validation import is_profile_ready
from src.sources.base import JobSource, Vacancy
from src.sources.career_sites import CareerSiteSource
from src.sources.telegram_channel import TelegramChannelSource
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from src.services.access import has_access, DAILY_LIMITS

logger = structlog.get_logger(__name__)

MAX_VACANCIES_PER_USER_PER_CYCLE = 100
MAX_DELIVERIES_PER_USER_PER_CYCLE = 8
USER_CONCURRENCY = 3
MAX_VACANCIES_PER_SOURCE = 50
# Buffer-mode test users (опытная группа). Через запятую в env.
BUFFER_MODE = os.getenv("BUFFER_MODE", "off").lower()
BUFFER_TEST_USERS = set(
    int(x) for x in os.getenv("BUFFER_TEST_USERS", "").split(",") if x.strip()
)

# Сколько всего циклов в день (синхронизировать с scheduler в main.py)
CYCLES_PER_DAY = int(os.getenv("CYCLES_PER_DAY", "3"))


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
    use_buffer = (
        BUFFER_MODE == "all"
        or (BUFFER_MODE == "test" and user.telegram_id in BUFFER_TEST_USERS)
    )
    if use_buffer:
        return await _process_user_with_buffer(bot, user, log)
    now_utc = datetime.now(timezone.utc)

async def _process_user_with_buffer(bot: Bot, user: User, log) -> dict:
    """Buffer-mode pipeline. Matching runs once per day (first cycle of the day),
    delivery happens every cycle from the persistent buffer.

    Buffer = VacancyMatch records with delivered_at IS NULL.
    """
    # Access gate — нет подписки и не grandfather, пропускаем.
    # Даже накопленный буфер не доставляется тем, кто потерял доступ.
    if not has_access(user):
        log.info(
            "skip_no_access_buffer",
            user_id=user.id,
            telegram_username=user.telegram_username,
        )
        return {"fetched": 0, "matched": 0, "delivered": 0}

    # Subscription gate: если канал настроен и юзер отписался — пропускаем
    from src.services.subscription import (
        is_required_channel_configured,
        is_subscribed,
        notify_if_unsubscribed,
    )
    if is_required_channel_configured():
        subscribed = await is_subscribed(bot, user.telegram_id)
        if not subscribed:
            log.info("skip_not_subscribed")
            # Одноразовая нотификация с 7-дневным cooldown
            await notify_if_unsubscribed(bot, user)
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
        ready, reason = is_profile_ready(profile.profile_data)
        if not ready:
            log.info("skip_incomplete_profile", reason=reason)
            return {"fetched": 0, "matched": 0, "delivered": 0}

        # 2. List active sources
        sources = await list_user_sources(session, user.id)
        if not sources:
            log.info("skip_no_sources")
            return {"fetched": 0, "matched": 0, "delivered": 0}

        # Access gate — если нет подписки и не grandfather, пропускаем юзера.
        # Пусть ждёт триггера от middleware (paywall notice) через собственное сообщение боту.
        if not has_access(user):
            log.info(
                "skip_no_access",
                user_id=user.id,
                telegram_username=user.telegram_username,
            )
            return {"fetched": 0, "matched": 0, "delivered": 0}

        # 3. Fetch from all sources
        all_fetched = await _fetch_from_all_sources(sources)
        log.info("fetched", count=len(all_fetched))

        # 4. Dedupe against history (and within the batch)
        fresh = await filter_unseen(session, user.id, all_fetched)
        log.info("fresh_after_dedup", count=len(fresh))

        if not fresh:
            return {"fetched": len(all_fetched), "matched": 0, "delivered": 0}

        # 5. Rank by lexical overlap with profile, then cap.
        # Free — 50 ваков в матчинг, Pro/Grandfather — 100.
        from src.services.prefilter import rank_vacancies
        ranked = rank_vacancies(fresh, profile.profile_data)
        is_paid = user.plan == "grandfather" or (
            user.plan == "pro" and user.plan_expires_at and user.plan_expires_at > now_utc
        )
        user_cap = MAX_VACANCIES_PER_USER_PER_CYCLE if is_paid else MAX_VACANCIES_PER_USER_PER_CYCLE // 2
        to_match = ranked[:user_cap]
        deferred = fresh[user_cap:]
        log.info("matching", count=len(to_match), deferred=len(deferred), user_cap=user_cap)

        # 6. Mark only matched items as seen. Deferred ones stay unseen
        # so they can be picked up in subsequent cycles, not lost forever.
        await mark_seen(session, user.id, to_match)
        await session.commit()

        # 7. Match each
        matcher = VacancyMatcher()
        deliveries: list[tuple[Vacancy, MatchResult]] = []
        all_scores: list[float] = []
        for position, vacancy in enumerate(to_match, 1):
            try:
                match = await matcher.match(profile.profile_data, vacancy)
                if match.should_send:
                    log.info("match_position", user_id=user.id, position=position, score=match.score, total_evals=len(to_match))
            except Exception as e:
                log.warning("match_failed", url=vacancy.url, error=str(e))
                continue
            all_scores.append(match.score)
            if match.should_send:
                deliveries.append((vacancy, match))

        if all_scores:
            buckets = {"9-10": 0, "8-9": 0, "7-8": 0, "6-7": 0, "5-6": 0, "4-5": 0, "<4": 0}
            for s in all_scores:
                if s >= 9: buckets["9-10"] += 1
                elif s >= 8: buckets["8-9"] += 1
                elif s >= 7: buckets["7-8"] += 1
                elif s >= 6: buckets["6-7"] += 1
                elif s >= 5: buckets["5-6"] += 1
                elif s >= 4: buckets["4-5"] += 1
                else: buckets["<4"] += 1
            log.info("score_distribution", **buckets)

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
                    text=format_vacancy_message(vacancy, match, match_id=vm.id, user_plan=user.plan),
                    reply_markup=build_reaction_keyboard(vm.id, user_plan=user.plan),
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=False,
                )
                sent_count += 1
                await asyncio.sleep(0.5)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                log.info("user_blocked_bot", user_id=user.id, error=str(e))
                await session.execute(
                    update(User)
                    .where(User.id == user.id)
                    .values(is_active=False)
                )
                user.is_active = False
                await session.commit()
                return {"fetched": len(all_fetched), "matched": len(to_match), "delivered": sent_count}
            except Exception as e:
                log.warning("delivery_failed", url=vacancy.url, error=str(e))
                continue

        await session.commit()
        log.info("user_done", delivered=sent_count)
        return {"fetched": len(all_fetched), "matched": len(to_match), "delivered": sent_count}

async def _estimate_cycles_done(session, user_id: int, today_start, now_utc) -> int:
    """Грубо оцениваем сколько циклов доставки уже было сегодня.
    Цикл = группа доставок в окне 1 час.
    """
    result = await session.execute(
        select(VacancyMatch.delivered_at)
        .where(VacancyMatch.user_id == user_id)
        .where(VacancyMatch.delivered_at >= today_start)
        .order_by(VacancyMatch.delivered_at)
    )
    timestamps = [r[0] for r in result.all()]
    if not timestamps:
        return 0

    # Считаем количество "кластеров" доставок — между группами >1 часа
    cycles = 1
    prev = timestamps[0]
    for ts in timestamps[1:]:
        if (ts - prev) > timedelta(hours=1):
            cycles += 1
        prev = ts
    return cycles

async def _fetch_from_all_sources(sources: list[Source]) -> list[Vacancy]:
    """Fetch from every source in parallel, with per-source cap and timeout."""
    tg = TelegramChannelSource()
    cs = CareerSiteSource()
    SOURCE_TIMEOUT = 30

    async def fetch_one(s: Source) -> list[Vacancy]:
        try:
            if s.source_type == SourceType.telegram_channel:
                vacancies = await asyncio.wait_for(tg.fetch(s), timeout=SOURCE_TIMEOUT)
            elif s.source_type == SourceType.career_site:
                vacancies = await asyncio.wait_for(cs.fetch(s), timeout=SOURCE_TIMEOUT)
            else:
                return []

            # Per-source cap: shuffle and take first N to avoid big sources dominating
            if len(vacancies) > MAX_VACANCIES_PER_SOURCE:
                random.shuffle(vacancies)
                vacancies = vacancies[:MAX_VACANCIES_PER_SOURCE]
            return vacancies
        except asyncio.TimeoutError:
            logger.warning("source_timeout", identifier=s.identifier)
            return []
        except Exception as e:
            logger.warning("source_failed", identifier=s.identifier, error=str(e))
            return []

    chunks = await asyncio.gather(*(fetch_one(s) for s in sources))
    combined: list[Vacancy] = []
    for chunk in chunks:
        combined.extend(chunk)
    return combined