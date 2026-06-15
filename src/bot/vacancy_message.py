"""Formatting and inline keyboard for vacancy delivery messages."""

from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.agents.matcher import MatchResult
from src.sources.base import Vacancy


def _escape_md(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    if not text:
        return ""
    chars = r"_*[]()~`>#+-=|{}.!\\"
    return "".join(("\\" + ch) if ch in chars else ch for ch in str(text))


def _score_emoji(score: float) -> str:
    if score >= 8.5:
        return "🔥"
    if score >= 7.0:
        return "✨"
    if score >= 6.0:
        return "👀"
    return "💭"


def format_vacancy_message(vacancy: Vacancy, match: MatchResult) -> str:
    """Build a clean MarkdownV2 message for one matched vacancy."""
    parts = []

    emoji = _score_emoji(match.score)
    score_str = f"{match.score:.1f}".replace(".", "\\.")
    parts.append(f"{emoji} *{score_str}/10*  {_escape_md(vacancy.title)}")
    parts.append("")

    meta_lines = []
    if vacancy.company:
        meta_lines.append(f"🏢 {_escape_md(vacancy.company)}")
    if vacancy.location:
        meta_lines.append(f"📍 {_escape_md(vacancy.location)}")
    if vacancy.salary:
        meta_lines.append(f"💰 {_escape_md(vacancy.salary)}")
    if meta_lines:
        parts.extend(meta_lines)
        parts.append("")

    if match.fit_reason:
        parts.append(f"*Почему подходит:*\n{_escape_md(match.fit_reason)}")
        parts.append("")

    if match.red_flags:
        parts.append("*⚠️ Обрати внимание:*")
        for flag in match.red_flags[:5]:
            parts.append(f"• {_escape_md(flag)}")
        parts.append("")

    parts.append(f"[Открыть вакансию]({_escape_url(vacancy.url)})")

    return "\n".join(parts)


def _escape_url(url: str) -> str:
    """For MarkdownV2 link URL, only ) and \\ need escaping."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def build_reaction_keyboard(match_id: int) -> InlineKeyboardMarkup:
    """Inline buttons for 👍/👎/откликнулась feedback."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 интересно", callback_data=f"react:liked:{match_id}"),
                InlineKeyboardButton(text="👎 не моё", callback_data=f"react:disliked:{match_id}"),
            ],
            [
                InlineKeyboardButton(text="📨 откликнулась", callback_data=f"react:applied:{match_id}"),
            ],
        ]
    )