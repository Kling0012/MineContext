"""FastAPI application: sync-server API (/v1)."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import uuid
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text as sql_text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import config
from .db import engine, get_db
from .embedding import embed_sync, embedding_worker, pending_count
from .models import Device, Event, EVENT_KINDS
from .schemas import (
    BatchResult,
    DayCounts,
    DayResponse,
    DeviceIn,
    EventBatchIn,
    EventOut,
    EventsPage,
    HealthResponse,
    SearchHit,
    SearchIn,
    SearchResponse,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sync.api")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(embedding_worker())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="MineContext sync-server", lifespan=lifespan)


def _ensure_device(db: Session, device_id: str, name: str = "", os_: str = "") -> None:
    stmt = (
        pg_insert(Device)
        .values(id=device_id, name=name, os=os_)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    db.execute(stmt)


@app.get("/healthz", response_model=HealthResponse)
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(sql_text("SELECT 1"))
        db_ok = "ok"
    except Exception as e:  # noqa: BLE001
        log.error("healthz db check failed: %s", e)
        db_ok = "fail"
    return HealthResponse(ok=db_ok == "ok", db=db_ok, embed_pending=pending_count())


@app.put("/v1/devices/{device_id}")
def put_device(device_id: str, body: DeviceIn, db: Session = Depends(get_db)):
    stmt = pg_insert(Device).values(
        id=device_id, name=body.name, os=body.os, last_seen_at=func.now()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={"name": body.name, "os": body.os, "last_seen_at": func.now()},
    )
    db.execute(stmt)
    db.commit()
    device = db.get(Device, device_id)
    return jsonable_encoder(device)


@app.post("/v1/events:batch", response_model=BatchResult)
def post_events_batch(body: EventBatchIn, db: Session = Depends(get_db)):
    _ensure_device(db, body.device_id)
    incoming = {ev.id: ev for ev in body.events}
    existing = set(
        db.scalars(select(Event.id).where(Event.id.in_(incoming.keys()))).fetchall()
    )
    for ev in body.events:
        stmt = pg_insert(Event).values(
            id=ev.id,
            device_id=body.device_id,
            kind=ev.kind,
            started_at=ev.started_at,
            ended_at=ev.ended_at,
            app=ev.app,
            title=ev.title,
            text=ev.text,
            meta=ev.meta,
            source=ev.source,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "device_id": body.device_id,
                "kind": ev.kind,
                "started_at": ev.started_at,
                "ended_at": ev.ended_at,
                "app": ev.app,
                "title": ev.title,
                "text": ev.text,
                "meta": ev.meta,
                "source": ev.source,
                "updated_at": func.now(),
            },
        )
        db.execute(stmt)
    db.commit()
    updated = len(existing & set(incoming.keys()))
    accepted = len(incoming) - updated
    return BatchResult(accepted=accepted, updated=updated)


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        ts_s, id_s = raw.split("|", 1)
        return datetime.fromisoformat(ts_s), uuid.UUID(id_s)
    except Exception as e:
        raise HTTPException(status_code=400, detail="invalid cursor") from e


def _encode_cursor(ts: datetime, id_: uuid.UUID) -> str:
    raw = f"{ts.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@app.get("/v1/events", response_model=EventsPage)
def list_events(
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    device_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    q = select(Event).order_by(Event.started_at.asc(), Event.id.asc())
    if from_ is not None:
        q = q.where(Event.started_at >= from_)
    if to is not None:
        q = q.where(Event.started_at < to)
    if device_id is not None:
        q = q.where(Event.device_id == device_id)
    if kind is not None:
        if kind not in EVENT_KINDS:
            raise HTTPException(status_code=422, detail=f"unknown kind: {kind}")
        q = q.where(Event.kind == kind)
    if cursor:
        ts, id_ = _decode_cursor(cursor)
        q = q.where(tuple_(Event.started_at, Event.id) > (ts, id_))
    q = q.limit(limit)
    events = db.scalars(q).fetchall()
    next_cursor = (
        _encode_cursor(events[-1].started_at, events[-1].id)
        if len(events) == limit
        else None
    )
    return EventsPage(
        events=[EventOut.model_validate(e) for e in events], next_cursor=next_cursor
    )


@app.get("/v1/days/{day}", response_model=DayResponse)
def get_day(
    day: date,
    tz: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    tz_name = tz or config.TZ_DEFAULT
    try:
        zone = ZoneInfo(tz_name)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"unknown tz: {tz_name}") from e
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = datetime.combine(day, time.max, tzinfo=zone).replace(
        microsecond=999999
    )
    q = (
        select(Event, Device.name)
        .join(Device, Event.device_id == Device.id, isouter=True)
        .where(Event.started_at >= start_local, Event.started_at <= end_local)
        .order_by(Event.started_at.asc(), Event.id.asc())
    )
    rows = db.execute(q).fetchall()
    events: list[EventOut] = []
    for ev, device_name in rows:
        out = EventOut.model_validate(ev)
        out.device_name = device_name
        events.append(out)
    count_rows = db.execute(
        select(Event.kind, func.count())
        .where(Event.started_at >= start_local, Event.started_at <= end_local)
        .group_by(Event.kind)
    ).fetchall()
    counts = [DayCounts(kind=k, count=c) for k, c in count_rows]
    return DayResponse(
        date=day.isoformat(),
        tz=tz_name,
        from_at=start_local.astimezone(timezone.utc),
        to_at=end_local.astimezone(timezone.utc),
        events=events,
        counts=counts,
    )


def _search_filters(params: dict, kinds, device_ids, from_, to) -> list[str]:
    """Shared SQL conditions for the vector and trgm sides of hybrid search."""
    conds = []
    if from_ is not None:
        conds.append("e.started_at >= :from_at")
        params["from_at"] = from_
    if to is not None:
        conds.append("e.started_at < :to_at")
        params["to_at"] = to
    if kinds:
        conds.append("e.kind = ANY(:kinds)")
        params["kinds"] = list(kinds)
    if device_ids:
        conds.append("e.device_id = ANY(:device_ids)")
        params["device_ids"] = list(device_ids)
    return conds


def _where(conds: list[str]) -> str:
    return (" WHERE " + " AND ".join(conds)) if conds else ""


@app.post("/v1/search", response_model=SearchResponse)
def search(body: SearchIn, db: Session = Depends(get_db)):
    for k in body.kinds or []:
        if k not in EVENT_KINDS:
            raise HTTPException(status_code=422, detail=f"unknown kind: {k}")
    params: dict = {"q": body.query}
    shared = _search_filters(params, body.kinds, body.device_ids, body.from_, body.to)

    # Vector side: cosine distance over events that have embeddings.
    # Runs on its own connection so a failure here never poisons the
    # transaction used by the trgm side below.
    try:
        qvec = embed_sync([body.query])[0]
        params["qv"] = "[" + ",".join(f"{x:.8g}" for x in qvec) + "]"
        with engine.connect() as conn:
            vec_rows = conn.execute(
                sql_text(
                    "SELECT ee.event_id FROM event_embeddings ee "
                    "JOIN events e ON e.id = ee.event_id"
                    + _where(shared)
                    + " ORDER BY ee.embedding <=> CAST(:qv AS vector) LIMIT 50"
                ),
                params,
            ).fetchall()
    except Exception as e:  # noqa: BLE001 - vector side is best-effort
        log.warning("vector search skipped: %s", e)
        vec_rows = []
    vec_ranks = {row[0]: i + 1 for i, row in enumerate(vec_rows)}

    # trgm side: similarity ranking, catches events without embeddings.
    trgm_rows = db.execute(
        sql_text(
            "SELECT e.id FROM events e"
            + _where(shared + ["similarity(e.text, :q) > 0.01"])
            + " ORDER BY similarity(e.text, :q) DESC, e.id LIMIT 50"
        ),
        params,
    ).fetchall()
    trgm_ranks = {row[0]: i + 1 for i, row in enumerate(trgm_rows)}

    rrf_k = 60
    scores: dict[uuid.UUID, float] = {}
    for ranks in (vec_ranks, trgm_ranks):
        for eid, rank in ranks.items():
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (rrf_k + rank)
    top = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[: body.k]
    if not top:
        return SearchResponse(query=body.query, hits=[])

    ids = [t[0] for t in top]
    rows = db.execute(
        select(Event, Device.name)
        .join(Device, Event.device_id == Device.id, isouter=True)
        .where(Event.id.in_(ids))
    ).fetchall()
    by_id = {ev.id: (ev, name) for ev, name in rows}
    hits = []
    for eid, score in top:
        if eid not in by_id:
            continue
        ev, name = by_id[eid]
        out = EventOut.model_validate(ev)
        out.device_name = name
        hits.append(
            SearchHit(
                event=out,
                score=score,
                vector_rank=vec_ranks.get(eid),
                trgm_rank=trgm_ranks.get(eid),
            )
        )
    return SearchResponse(query=body.query, hits=hits)


@app.delete("/v1/events/{event_id}")
def delete_event(event_id: uuid.UUID, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    db.delete(ev)
    db.commit()
    return {"deleted": str(event_id)}
