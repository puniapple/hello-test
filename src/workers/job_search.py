"""Main job-search cycle: fetch -> dedup -> match -> send."""

from __future__ import annotations

import asyncio
import logging
import random
import os
from datetime import datetime, timezone, timedelta

import structlog
from aiogram import Bot
from sqlalchemy import select, func
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

MAX_VACANCIES_PER_USER_PER_CYCLE = 50
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

        # 5. Rank by lexical overlap with profile, then cap. Niche users see
        # their relevant vacancies even when the pool is broad.
        from src.services.prefilter import rank_vacancies
        ranked = rank_vacancies(fresh, profile.profile_data)
        to_match = ranked[:MAX_VACANCIES_PER_USER_PER_CYCLE]
        deferred = fresh[MAX_VACANCIES_PER_USER_PER_CYCLE:]
        log.info("matching", count=len(to_match), deferred=len(deferred))

        # 6. Mark only matched items as seen. Deferred ones stay unseen
        # so they can be picked up in subsequent cycles, not lost forever.
        await mark_seen(session, user.id, to_match)
        await session.commit()

        # 7. Match each
        matcher = VacancyMatcher()
        deliveries: list[tuple[Vacancy, MatchResult]] = []
        all_scores: list[float] = []
        for vacancy in to_match:
            try:
                match = await matcher.match(profile.profile_data, vacancy)
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

async def _process_user_with_buffer(bot: Bot, user: User, log) -> dict:
    """Buffer-mode pipeline. Matching runs once per day (first cycle of the day),
    delivery happens every cycle from the persistent buffer.

    Buffer = VacancyMatch records with delivered_at IS NULL.
    """
    # Subscription gate (как в обычной функции)
    from src.services.subscription import is_required_channel_configured, is_subscribed
    if is_required_channel_configured():
        subscribed = await is_subscribed(bot, user.telegram_id)
        if not subscribed:
            log.info("skip_not_subscribed_buffer")
            return {"fetched": 0, "matched": 0, "delivered": 0}

    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    async with async_session() as session:
        # Определяем: это первый цикл за день?
        is_matching_cycle = (
            user.last_match_cycle_at is None
            or user.last_match_cycle_at < today_start
        )
        log.info(
            "buffer_cycle_decision",
            is_matching_cycle=is_matching_cycle,
            last_match_cycle_at=str(user.last_match_cycle_at) if user.last_match_cycle_at else None,
        )

        # ─── Часть 1: матчинг (только в первом цикле дня) ───
        matched_count = 0
        fetched_count = 0
        if is_matching_cycle:
            # 1. Load profile
            profile_result = await session.execute(
                select(Profile).where(Profile.user_id == user.id)
            )
            profile = profile_result.scalar_one_or_none()
            if profile is None or not profile.profile_data:
                log.info("skip_no_profile_buffer")
                # Не выходим — может быть в буфере есть что доставить
            else:
                # 2. Sources
                sources = await list_user_sources(session, user.id)
                if sources:
                    # 3. Fetch
                    all_fetched = await _fetch_from_all_sources(sources)
                    fetched_count = len(all_fetched)
                    log.info("fetched_buffer", count=fetched_count)

                    # 4. Dedupe
                    fresh = await filter_unseen(session, user.id, all_fetched)
                    log.info("fresh_after_dedup_buffer", count=len(fresh))

                    if fresh:
                        # 5. Pre-filter + cap
                        from src.services.prefilter import rank_vacancies
                        ranked = rank_vacancies(fresh, profile.profile_data)
                        to_match = ranked[:MAX_VACANCIES_PER_USER_PER_CYCLE]
                        log.info("matching_buffer", count=len(to_match))

                        # 6. Mark seen
                        await mark_seen(session, user.id, to_match)
                        await session.commit()

                        # 7. Match each → save to buffer (delivered_at = NULL)
                        matcher = VacancyMatcher()
                        all_scores: list[float] = []
                        for vacancy in to_match:
                            try:
                                match = await matcher.match(profile.profile_data, vacancy)
                            except Exception as e:
                                log.warning("match_failed_buffer", url=vacancy.url, error=str(e))
                                continue
                            all_scores.append(match.score)
                            if match.should_send:
                                # Сохраняем в буфер БЕЗ доставки
                                vm = VacancyMatch(
                                    user_id=user.id,
                                    vacancy_hash=vacancy.hash,
                                    vacancy_data=vacancy.to_storage_dict(),
                                    match_score=match.score,
                                    match_reason=match.fit_reason,
                                    delivered_at=None,  # явно в буфере
                                )
                                session.add(vm)
                                matched_count += 1

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
                            log.info("score_distribution_buffer", **buckets)

                        await session.commit()
                        log.info("buffer_filled", new_in_buffer=matched_count)

                        # Записываем что матчинг сегодня уже был — даже если в буфер ничего не легло
                        user.last_match_cycle_at = now_utc
                        await session.commit()


        # 8. Cleanup: удалить из буфера ваки старше 48 часов
        expiry_cutoff = now_utc - timedelta(hours=48)
        expired_result = await session.execute(
            select(VacancyMatch)
            .where(VacancyMatch.user_id == user.id)
            .where(VacancyMatch.delivered_at.is_(None))
            .where(VacancyMatch.sent_at < expiry_cutoff)
        )
        expired = expired_result.scalars().all()
        for vm in expired:
            await session.delete(vm)
        if expired:
            await session.commit()
            log.info("buffer_expired_removed", count=len(expired))

        # ─── Часть 2: доставка из буфера (всегда) ───
        # Загружаем весь буфер юзера, отсортированный по скору
        buffer_result = await session.execute(
            select(VacancyMatch)
            .where(VacancyMatch.user_id == user.id)
            .where(VacancyMatch.delivered_at.is_(None))
            .order_by(VacancyMatch.match_score.desc())
        )
        buffer = buffer_result.scalars().all()
        log.info("buffer_size", count=len(buffer))

        # Сколько циклов осталось до конца дня (включая текущий)
        if is_matching_cycle:
            # Только что отработали матчинг — это и есть первый цикл
            remaining_cycles = CYCLES_PER_DAY
        else:
            # Грубая оценка: цикл = группа доставок в течение часа
            # Считаем что между циклами >1 часа
            cycles_done = await _estimate_cycles_done(session, user.id, today_start, now_utc)
            remaining_cycles = max(1, CYCLES_PER_DAY - cycles_done)

        # Сколько отправить сейчас
        if len(buffer) == 0:
            to_send_now = 0
        else:
            # Равномерное распределение, минимум 1 если есть ваки
            to_send_now = max(1, len(buffer) // remaining_cycles)
            # Крышка — Pro 5, Free 3. На тесте у тебя Pro.
            to_send_now = min(to_send_now, 5)

        log.info(
            "delivery_plan",
            buffer=len(buffer),
            remaining_cycles=remaining_cycles,
            to_send_now=to_send_now,
        )

        # Доставляем
        sent_count = 0
        for vm in buffer[:to_send_now]:
            try:
                # Восстанавливаем Vacancy из vacancy_data для format_vacancy_message
                vacancy = Vacancy.from_storage_dict(vm.vacancy_data)
                match_result = MatchResult(
                    score=vm.match_score,
                    fit_reason=vm.match_reason,
                    red_flags=[],
                    should_send=True,
                )

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=format_vacancy_message(vacancy, match_result),
                    reply_markup=build_reaction_keyboard(vm.id),
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=False,
                )
                vm.delivered_at = now_utc
                sent_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning("delivery_failed_buffer", url=vm.vacancy_data.get("url"), error=str(e))
                continue

        await session.commit()
        log.info("user_done_buffer", delivered=sent_count, buffer_remaining=len(buffer) - sent_count)
        return {"fetched": fetched_count, "matched": matched_count, "delivered": sent_count}


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