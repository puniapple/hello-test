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
    profile_ready_for_search: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # --- Tribute subscription fields ---
    plan: Mapped[str] = mapped_column(String(32), default="free", server_default="free")
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subscription_status: Mapped[str] = mapped_column(String(32), default="free", server_default="free")
    tribute_order_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_payment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expiry_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    profile: Mapped["Profile"] = relationship(back_populates="user", uselist=False)


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