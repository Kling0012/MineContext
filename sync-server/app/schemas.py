"""Pydantic request/response schemas."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from .models import EVENT_KINDS


class DeviceIn(BaseModel):
    name: str = ""
    os: str = ""


class EventIn(BaseModel):
    id: uuid.UUID
    kind: str = Field(pattern="^(activity|screen_summary|note|todo|ai_work|report)$")
    started_at: datetime
    ended_at: datetime | None = None
    app: str | None = None
    title: str | None = None
    text: str
    meta: dict = Field(default_factory=dict)
    source: str | None = None


class EventBatchIn(BaseModel):
    device_id: str
    events: list[EventIn] = Field(max_length=500)


class BatchResult(BaseModel):
    accepted: int
    updated: int


class EventOut(BaseModel):
    id: uuid.UUID
    device_id: str
    device_name: str | None = None
    kind: str
    started_at: datetime
    ended_at: datetime | None
    app: str | None
    title: str | None
    text: str
    meta: dict
    source: str | None
    session_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EventsPage(BaseModel):
    events: list[EventOut]
    next_cursor: str | None = None


class DayCounts(BaseModel):
    kind: str
    count: int


class DayResponse(BaseModel):
    date: str
    tz: str
    from_at: datetime
    to_at: datetime
    events: list[EventOut]
    counts: list[DayCounts]


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    kinds: list[str] | None = None
    device_ids: list[str] | None = None
    k: int = Field(default=20, ge=1, le=200)


class SearchHit(BaseModel):
    event: EventOut
    score: float
    vector_rank: int | None = None
    trgm_rank: int | None = None


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]


class HealthResponse(BaseModel):
    ok: bool
    db: str
    embed_pending: int


class SessionCreateIn(BaseModel):
    title: str = Field(min_length=1)
    note: str | None = None
    device_id: str | None = None
    started_at: datetime | None = None  # default: now


class SessionEndIn(BaseModel):
    ended_at: datetime | None = None  # default: now


class SessionPatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    note: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    confirmed: bool | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    device_id: str | None
    title: str
    note: str | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    end_reason: str | None
    end_confidence: float | None
    end_evidence: list[dict]
    confirmed: bool
    duration_minutes: float = 0.0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionCreateOut(BaseModel):
    session: SessionOut
    superseded: SessionOut | None = None


class SessionsPage(BaseModel):
    sessions: list[SessionOut]


class ActiveSessionOut(BaseModel):
    session: SessionOut | None = None


class SessionTotal(BaseModel):
    session_id: uuid.UUID
    title: str
    status: str
    confirmed: bool
    started_at: datetime
    ended_at: datetime | None
    minutes: float


class TimelineItem(BaseModel):
    type: str  # "session" | "event" | "ai_work"
    t: datetime
    event: EventOut | None = None
    session: SessionOut | None = None


class TimelineResponse(BaseModel):
    date: str
    tz: str
    from_at: datetime
    to_at: datetime
    items: list[TimelineItem]
    session_totals: list[SessionTotal]
    total_minutes: float


class DailyStat(BaseModel):
    """One calendar day (in the requested tz) for GET /v1/stats/daily."""

    date: str
    events: int  # all kinds
    mine: int  # kind != ai_work
    ai_work: int
    # distinct 5-min buckets of started_at x 5
    active_minutes: int  # kind != ai_work
    ai_active_minutes: int  # same for ai_work
    # work_sessions (cancelled excluded) overlapping the day, clipped at
    # day boundaries; active sessions count up to now
    session_minutes: float
    first_at: datetime | None
    last_at: datetime | None


class DailyStatsResponse(BaseModel):
    tz: str
    days: list[DailyStat]


class ImporterOut(BaseModel):
    name: str
    enabled: bool = False
    cursor_ts: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    imported_count: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class ImportersPage(BaseModel):
    importers: list[ImporterOut]


class ImporterRunOut(BaseModel):
    ok: bool
    fetched: int
    upserted: int
    pages: int
    cursor_ts: datetime | None
