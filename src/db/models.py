"""Database models."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sqlalchemy import String, Integer, BigInteger, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

class Base(DeclarativeBase):
    pass


class UserState(str, enum.Enum):
    idle = "idle"
    editing_profile = "editing_profile"
    paused = "paused"


class ChatRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class ChatContext(str, enum.Enum):
    profile_edit = "profile_edit"
    onboarding = "onboarding"
    general = "general"


class SourceType(str, enum.Enum):
    hh_ru = "hh_ru"
    telegram_channel = "telegram_channel"
    career_site = "career_site"


class UserReaction(str, enum.Enum):
    liked = "liked"
    disliked = "disliked"
    applied = "applied"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[UserState] = mapped_column(
        Enum(UserState, name="user_state"), default=UserState.idle
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_match_cycle_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profile_ready_for_search: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # --- Tribute subscription fields ---
    plan: Mapped[str] = mapped_column(String(32), default="free", server_default="free")
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subscription_status: Mapped[str] = mapped_column(String(32), default="free", server_default="free")
    empty_streak_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    empty_notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tribute_subscription_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    last_payment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expiry_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gate_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_paywall_notice: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    free_breakdown_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    profile: Mapped["Profile"] = relationship(back_populates="user", uselist=False)

    survey_awaiting: Mapped[str | None] = mapped_column(String(64), default=None, nullable=True)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    profile_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="profile")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[ChatRole] = mapped_column(Enum(ChatRole, name="chat_role"))
    content: Mapped[str] = mapped_column(Text)
    context: Mapped[ChatContext] = mapped_column(
        Enum(ChatContext, name="chat_context"), default=ChatContext.general
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType, name="source_type"))
    identifier: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SeenVacancy(Base):
    __tablename__ = "seen_vacancies"
    __table_args__ = (
        UniqueConstraint("user_id", "vacancy_hash", name="uq_user_vacancy"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    vacancy_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType, name="source_type"))
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    global_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VacancyMatch(Base):
    __tablename__ = "vacancy_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    vacancy_hash: Mapped[str] = mapped_column(String(64), index=True)
    vacancy_data: Mapped[dict] = mapped_column(JSONB)
    match_score: Mapped[float] = mapped_column(Float)
    match_reason: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_reaction: Mapped[UserReaction | None] = mapped_column(
        Enum(UserReaction, name="user_reaction"), nullable=True
    )
    
class TributeWebhookEvent(Base):
    __tablename__ = "tribute_webhook_events"
    __table_args__ = (
        UniqueConstraint("event_name", "order_uuid", "sent_at", name="uq_tribute_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_name: Mapped[str] = mapped_column(String(64))
    order_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    signature_valid: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

class CoverLetterUsage(Base):
    """Логирует каждое сгенерированное сопроводительное письмо.

    Используется для rolling-window rate limit (24ч) — считаем COUNT(*)
    записей за последние 24 часа для user_id, сравниваем с лимитом тарифа.
    """
    __tablename__ = "cover_letter_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vacancy_match_id: Mapped[int | None] = mapped_column(
        ForeignKey("vacancy_matches.id", ondelete="SET NULL"),
        nullable=True,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

class ResumeUsage(Base):
    """Логирует каждое сгенерированное адаптированное резюме.

    Для Free — лимит 1 за всю жизнь юзера (COUNT все записи).
    Для Pro/Grandfather — лимит 3 в скользящем окне 24ч.
    """
    __tablename__ = "resume_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vacancy_match_id: Mapped[int | None] = mapped_column(
        ForeignKey("vacancy_matches.id", ondelete="SET NULL"),
        nullable=True,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

class ScoreBreakdown(Base):
    """Разбор соответствия вакансии профилю юзера.

    Генерится Sonnet по клику на кнопку "Оценить совпадение" (Free)
    или "Показать совпадения" (Pro/Grandfather). Кешируется навсегда:
    повторный клик на ту же вакансию не тратит триал у Free и не
    генерит заново у Pro.
    """
    __tablename__ = "score_breakdowns"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("vacancy_matches.id", ondelete="CASCADE"),
        unique=True,          # один разбор на матч
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    breakdown_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)  # проценты 0-100
    model_used: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

class SurveyResponse(Base):
    __tablename__ = "survey_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question_key: Mapped[str] = mapped_column(String(64), default="hard_job_search_072026")
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())