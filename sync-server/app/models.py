"""SQLAlchemy engine/session and table models."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pgvector.sqlalchemy import Vector

from . import config


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    os: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


EVENT_KINDS = (
    "activity",
    "screen_summary",
    "note",
    "todo",
    "ai_work",
    "report",
)

# Kinds that events:batch auto-links to the work session active at their time.
AUTO_LINK_KINDS = ("activity", "screen_summary")

SESSION_STATUSES = ("active", "ended", "cancelled")
SESSION_END_REASONS = ("manual", "ai_detected", "idle", "superseded")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('activity','screen_summary','note','todo','ai_work','report')",
            name="ck_events_kind",
        ),
        Index("ix_events_started_at", "started_at"),
        Index("ix_events_device_started", "device_id", "started_at"),
        Index("ix_events_kind", "kind"),
        Index("ix_events_session", "session_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    app: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    source: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkSession(Base):
    """A declared work session ("これから○○をする") whose end is judged later."""

    __tablename__ = "work_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','ended','cancelled')",
            name="ck_work_sessions_status",
        ),
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN "
            "('manual','ai_detected','idle','superseded')",
            name="ck_work_sessions_end_reason",
        ),
        Index("ix_work_sessions_status_started", "status", "started_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Device the session was declared on (plain text; may not be a known device).
    device_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    end_reason: Mapped[str | None] = mapped_column(Text)
    end_confidence: Mapped[float | None] = mapped_column(Float)
    end_evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'[]'::jsonb")
    )
    confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class EventEmbedding(Base):
    __tablename__ = "event_embeddings"
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # Dimension is fixed by EMBED_DIM at migration time.
    embedding = mapped_column(Vector(config.EMBED_DIM), nullable=False)
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ImporterState(Base):
    """Cursor/bookkeeping for a background importer (agent-memory, P3-A)."""

    __tablename__ = "importer_state"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    # Max created_at fetched so far (inclusive); NULL = never ran.
    cursor_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    imported_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
