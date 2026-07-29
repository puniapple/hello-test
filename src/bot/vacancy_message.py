"""Formatting and inline keyboard for vacancy delivery messages."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.agents.matcher import MatchResult
from src.sources.base import Vacancy


def _escape_md(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    if not text:
        return ""
    chars = r"_*[]()~`>#+-=|{}.!\\"
    return "".join(("\\" + ch) if ch in chars else ch for ch in str(text))


def _escape_url(url: str) -> str:
    """For MarkdownV2 link URL, only ) and \\ need escaping."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def _score_to_percent(score: float) -> int:
    """Скор 0-10 → проценты 0-100."""
    return max(0, min(100, int(round(score * 10))))


def format_vacancy_message(
    vacancy: Vacancy,
    match: MatchResult,
    user_plan: str = "free",
) -> str:
    """Собираем MarkdownV2 сообщение о вакансии.

    Free: без скора, короткое саммари, CTA на кнопку "Оценить совпадение".
    Pro/Grandfather: со скором в процентах, саммари, CTA на "Показать совпадения".
    """
    is_paid = user_plan.lower() in ("pro", "grandfather")

    parts = []
    # ── Заголовок ─────────────────────────────
    parts.append(f"✨ *{_escape_md(vacancy.title)}*")
    parts.append("")

    # ── Мета: компания, локация, зарплата ──
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

    # ── Саммари роли (одно предложение) ───
    # Теперь fit_reason — это описание сути роли, не оценка совпадения
    if match.fit_reason:
        parts.append(_escape_md(match.fit_reason))
        parts.append("")

    # ── Скор / CTA ────────────────────────
    if is_paid:
        percent = _score_to_percent(match.score)
        parts.append(f"*Вакансия подходит тебе на {percent}%*")
        parts.append("")
        parts.append(
            _escape_md(
                "Чтобы узнать, в чём ты подходишь компании, а где есть пробелы — "
                "нажми кнопку «Показать совпадения»"
            )
        )
    else:
        parts.append(
            _escape_md(
                "Чтобы узнать, насколько тебе подходит эта вакансия, "
                "нажми кнопку «Оценить совпадение»"
            )
        )
    parts.append("")

    # ── Ссылка ────────────────────────────
    parts.append(f"[Открыть вакансию]({_escape_url(vacancy.url)})")

    return "\n".join(parts)


def build_reaction_keyboard(
    match_id: int,
    user_plan: str = "free",
) -> InlineKeyboardMarkup:
    """Три кнопки под каждой вакансией:
    - "Оценить совпадение" (Free) / "Показать совпадения" (Pro) — первая
    - "Написать сопроводительное" — вторая
    - "Резюме под вакансию" — третья
    """
    is_paid = user_plan.lower() in ("pro", "grandfather")
    breakdown_label = "🎯 Показать совпадения" if is_paid else "🎯 Оценить совпадение"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=breakdown_label,
                callback_data=f"breakdown:generate:{match_id}",
            )],
            [InlineKeyboardButton(
                text="✍️ Написать сопроводительное",
                callback_data=f"cover:generate:{match_id}",
            )],
            [InlineKeyboardButton(
                text="📝 Резюме под вакансию",
                callback_data=f"resume:generate:{match_id}",
            )],
        ]
    )