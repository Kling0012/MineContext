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
