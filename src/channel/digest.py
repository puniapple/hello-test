"""Воскресный дайджест: лучшие вакансии, опубликованные в канале за 7 дней, по группам профессий.

run_digest(bot, dry_run=True)  — печатает дайджест, ничего не шлёт.
run_digest(bot, dry_run=False) — публикует в CHANNEL_ID.
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.channel import config as cfg
from src.db.session import async_session

logger = logging.getLogger(__name__)
TG_LIMIT = 4000  # запас от лимита Telegram в 4096 символов


async def _load_week() -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with async_session() as session:
        rows = await session.execute(
            text("SELECT score, message_id, card, created_at FROM posted_to_channel "
                 "WHERE status = 'posted' AND message_id IS NOT NULL AND card IS NOT NULL "
                 "AND created_at >= :since AND score >= :min_score"),
            {"since": since, "min_score": cfg.DIGEST_MIN_SCORE},
        )
        out = []
        for score, message_id, card, created_at in rows:
            if isinstance(card, str):
                card = json.loads(card)
            out.append({"score": score or 0, "message_id": message_id, "card": card or {},
                        "created_at": created_at})
        return out


def _group_of(tags: list[str]) -> str | None:
    tags = set(tags or [])
    for name, group_tags in cfg.DIGEST_GROUPS:
        if tags & set(group_tags):
            return name
    return None


def _post_link(message_id: int) -> str:
    return f"https://t.me/{cfg.CHANNEL_USERNAME}/{message_id}"


def build_digest(items: list[dict]) -> list[str]:
    """Возвращает список сообщений (обычно одно; два — если не влезло в лимит)."""
    grouped: dict[str, list[dict]] = {name: [] for name, _ in cfg.DIGEST_GROUPS}
    for it in items:
        g = _group_of(it["card"].get("tags"))
        if g:
            grouped[g].append(it)

    e = html.escape
    now = datetime.now(timezone.utc)
    period = f"{(now - timedelta(days=6)).strftime('%d.%m')}–{now.strftime('%d.%m')}"
    blocks = []
    for name, _ in cfg.DIGEST_GROUPS:
        best = sorted(grouped[name], key=lambda x: (x["score"], x["created_at"]), reverse=True)
        best = best[:cfg.DIGEST_PER_GROUP]
        if not best:
            continue
        lines = [f"<b>{e(name)}</b>"]
        for it in best:
            c = it["card"]
            tail = " · ".join(e(str(x)) for x in [c.get("company"), c.get("format")] if x)
            role = f'<a href="{_post_link(it["message_id"])}">{e(str(c.get("role") or "Вакансия"))}</a>'
            lines.append(f"— {role}" + (f" · {tail}" if tail else ""))
        blocks.append("\n".join(lines))

    if not blocks:
        return []

    header = f"<b>{e(cfg.DIGEST_TITLE)}</b> · {period}"
    messages, current = [], header
    for block in blocks:
        if len(current) + len(block) + 2 > TG_LIMIT:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    messages.append(current)
    return messages


async def run_digest(bot=None, dry_run: bool = True) -> None:
    items = await _load_week()
    messages = build_digest(items)
    print(f"═══ дайджест: вакансий за неделю {len(items)}, сообщений {len(messages)}", flush=True)
    if not messages:
        logger.warning("channel_digest_empty")
        return
    for msg in messages:
        if dry_run:
            print(msg + "\n", flush=True)
        else:
            await bot.send_message(cfg.CHANNEL_ID, msg, parse_mode="HTML", disable_web_page_preview=True)
