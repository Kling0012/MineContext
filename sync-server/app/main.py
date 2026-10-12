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
from sqlalchemy import func, or_, select, text as sql_text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import am_import, config
from .db import engine, get_db
from .embedding import embed_sync, embedding_worker, pending_count
from .links import session_links_for_times
from .models import (
    AUTO_LINK_KINDS,
    Device,
    Event,
    EVENT_KINDS,
    ImporterState,
    SESSION_STATUSES,
    WorkSession,
)
from .schemas import (
    ActiveSessionOut,
    BatchResult,
    DailyStat,
    DailyStatsResponse,
    DayCounts,
    DayResponse,
    DeviceIn,
    EventBatchIn,
    EventIn,
    EventOut,
    EventsPage,
    HealthResponse,
    ImporterOut,
    ImportersPage,
    ImporterRunOut,
    SearchHit,
    SearchIn,
    SearchResponse,
    SessionCreateIn,
    SessionCreateOut,
    SessionEndIn,
    SessionOut,
    SessionPatchIn,
    SessionTotal,
    SessionsPage,
    TimelineItem,
    TimelineResponse,
)
from .session_judge import session_judge_worker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sync.api")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(embedding_worker()),
        asyncio.create_task(session_judge_worker()),
        asyncio.create_task(am_import.am_import_worker()),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
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


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _session_links(db: Session, events: list[EventIn]) -> dict[uuid.UUID, uuid.UUID]:
    """activity/screen_summary -> the session active at the event's time."""
    return session_links_for_times(
        db,
        [
            (ev.id, _as_utc(ev.started_at))
            for ev in events
            if ev.kind in AUTO_LINK_KINDS
        ],
    )


@app.post("/v1/events:batch", response_model=BatchResult)
def post_events_batch(body: EventBatchIn, db: Session = Depends(get_db)):
    _ensure_device(db, body.device_id)
    links = _session_links(db, body.events)
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
            session_id=links.get(ev.id),
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
                "session_id": links.get(ev.id),
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


# --- Work sessions (P2-B): declare -> AI-judged end -> timeline ---


def _duration_minutes(s: WorkSession, now: datetime | None = None) -> float:
    end = s.ended_at or (now or datetime.now(timezone.utc))
    return max(0.0, (end - s.started_at).total_seconds() / 60.0)


def _session_out(s: WorkSession, now: datetime | None = None) -> SessionOut:
    out = SessionOut.model_validate(s)
    out.duration_minutes = round(_duration_minutes(s, now), 2)
    return out


def _get_session(db: Session, session_id: uuid.UUID) -> WorkSession:
    s = db.get(WorkSession, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@app.post("/v1/sessions", response_model=SessionCreateOut)
def create_session(body: SessionCreateIn, db: Session = Depends(get_db)):
    started = _as_utc(body.started_at or datetime.now(timezone.utc))
    superseded: WorkSession | None = None
    for old in db.scalars(
        select(WorkSession).where(WorkSession.status == "active")
    ).fetchall():
        old.status = "ended"
        old.end_reason = "superseded"
        old.ended_at = max(old.started_at, started)  # clamp for backdated declares
        superseded = old
    s = WorkSession(
        title=body.title,
        note=body.note,
        device_id=body.device_id,
        started_at=started,
    )
    db.add(s)
    db.commit()
    return SessionCreateOut(
        session=_session_out(s),
        superseded=_session_out(superseded) if superseded else None,
    )


@app.get("/v1/sessions/active", response_model=ActiveSessionOut)
def get_active_session(db: Session = Depends(get_db)):
    s = db.scalars(
        select(WorkSession)
        .where(WorkSession.status == "active")
        .order_by(WorkSession.started_at.desc())
        .limit(1)
    ).first()
    return ActiveSessionOut(session=_session_out(s) if s else None)


@app.get("/v1/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: uuid.UUID, db: Session = Depends(get_db)):
    return _session_out(_get_session(db, session_id))


@app.get("/v1/sessions", response_model=SessionsPage)
def list_sessions(
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    if status is not None and status not in SESSION_STATUSES:
        raise HTTPException(status_code=422, detail=f"unknown status: {status}")
    q = select(WorkSession).order_by(WorkSession.started_at.asc(), WorkSession.id.asc())
    if from_ is not None:
        q = q.where(WorkSession.started_at >= from_)
    if to is not None:
        q = q.where(WorkSession.started_at < to)
    if status is not None:
        q = q.where(WorkSession.status == status)
    q = q.limit(limit)
    now = datetime.now(timezone.utc)
    sessions = [_session_out(s, now) for s in db.scalars(q).fetchall()]
    return SessionsPage(sessions=sessions)


@app.post("/v1/sessions/{session_id}/end", response_model=SessionOut)
def end_session(session_id: uuid.UUID, body: SessionEndIn, db: Session = Depends(get_db)):
    s = _get_session(db, session_id)
    if s.status == "cancelled":
        raise HTTPException(status_code=409, detail="session is cancelled")
    ended = _as_utc(body.ended_at or datetime.now(timezone.utc))
    if ended < s.started_at:
        raise HTTPException(status_code=422, detail="ended_at before started_at")
    s.status = "ended"
    s.ended_at = ended
    s.end_reason = "manual"
    s.confirmed = True
    db.commit()
    return _session_out(s)


@app.patch("/v1/sessions/{session_id}", response_model=SessionOut)
def patch_session(
    session_id: uuid.UUID, body: SessionPatchIn, db: Session = Depends(get_db)
):
    s = _get_session(db, session_id)
    data = body.model_dump(exclude_unset=True)
    if "started_at" in data and data["started_at"] is not None:
        data["started_at"] = _as_utc(data["started_at"])
    if "ended_at" in data and data["ended_at"] is not None:
        data["ended_at"] = _as_utc(data["ended_at"])
    new_started = data.get("started_at", s.started_at)
    new_ended = data.get("ended_at", s.ended_at)
    if new_ended is not None and new_ended < new_started:
        raise HTTPException(status_code=422, detail="ended_at before started_at")
    for field in ("title", "note", "started_at", "ended_at", "confirmed"):
        if field in data:
            setattr(s, field, data[field])
    if "ended_at" in data:
        if data["ended_at"] is None:
            if s.status == "ended":  # clearing the end reopens the session
                s.status = "active"
                s.end_reason = None
                s.end_confidence = None
        elif s.status == "active":
            s.status = "ended"
            if s.end_reason is None:
                s.end_reason = "manual"
    db.commit()
    return _session_out(s)


@app.post("/v1/sessions/{session_id}/cancel", response_model=SessionOut)
def cancel_session(session_id: uuid.UUID, db: Session = Depends(get_db)):
    s = _get_session(db, session_id)
    if s.status != "cancelled":
        s.status = "cancelled"
    db.commit()
    return _session_out(s)


@app.get("/v1/timeline", response_model=TimelineResponse)
def get_timeline(
    date_: date = Query(alias="date"),
    tz: str | None = Query(default=None),
    include_superseded: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    tz_name = tz or config.TZ_DEFAULT
    try:
        zone = ZoneInfo(tz_name)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"unknown tz: {tz_name}") from e
    start_local = datetime.combine(date_, time.min, tzinfo=zone)
    end_local = datetime.combine(date_, time.max, tzinfo=zone).replace(
        microsecond=999999
    )
    window_start = start_local.astimezone(timezone.utc)
    window_end = end_local.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)

    sessions = db.scalars(
        select(WorkSession)
        .where(
            WorkSession.started_at <= window_end,
            or_(WorkSession.ended_at.is_(None), WorkSession.ended_at >= window_start),
        )
        .order_by(WorkSession.started_at.asc(), WorkSession.id.asc())
    ).fetchall()
    ev_q = (
        select(Event, Device.name)
        .join(Device, Event.device_id == Device.id, isouter=True)
        .where(Event.started_at >= window_start, Event.started_at <= window_end)
    )
    if not include_superseded:
        # Hide superseded ai_work records (the replacement is shown instead).
        # astext (->>) treats both a missing key and a JSON null as NULL.
        ev_q = ev_q.where(
            or_(Event.kind != "ai_work", Event.meta["superseded_by"].astext.is_(None))
        )
    rows = db.execute(
        ev_q.order_by(Event.started_at.asc(), Event.id.asc())
    ).fetchall()

    items: list[TimelineItem] = []
    for s in sessions:
        items.append(
            TimelineItem(type="session", t=s.started_at, session=_session_out(s, now))
        )
    for ev, device_name in rows:
        out = EventOut.model_validate(ev)
        out.device_name = device_name
        items.append(
            TimelineItem(
                type="ai_work" if ev.kind == "ai_work" else "event",
                t=ev.started_at,
                event=out,
            )
        )
    items.sort(
        key=lambda it: (
            it.t,
            0 if it.type == "session" else 1,
            str((it.session or it.event).id),
        )
    )
    totals = [
        SessionTotal(
            session_id=s.id,
            title=s.title,
            status=s.status,
            confirmed=s.confirmed,
            started_at=s.started_at,
            ended_at=s.ended_at,
            minutes=round(_duration_minutes(s, now), 2),
        )
        for s in sessions
        if s.status != "cancelled"
    ]
    return TimelineResponse(
        date=date_.isoformat(),
        tz=tz_name,
        from_at=window_start,
        to_at=window_end,
        items=items,
        session_totals=totals,
        total_minutes=round(sum(t.minutes for t in totals), 2),
    )


# --- Daily stats (heatmap): one row per calendar day in the requested tz ---


# One SQL: day grid (generate_series) + event aggregates + 5-min activity
# buckets + session minutes clipped at day boundaries. All days in the range
# are returned, including days with no records (LEFT JOIN -> zeros/NULL).
_DAILY_STATS_SQL = sql_text(
    """
    WITH day_list AS (
        -- pure calendar arithmetic (date + int): no session-TZ involvement
        SELECT (CAST(:from_d AS date) + n)::date AS d
        FROM generate_series(0, CAST(:to_d AS date) - CAST(:from_d AS date)) AS g(n)
    ),
    bounds AS (
        SELECT d,
               timezone(:tz, d::timestamp) AS s,
               timezone(:tz, (d + 1)::timestamp) AS e
        FROM day_list
    ),
    evday AS (
        SELECT b.d, e.kind, e.started_at,
               floor(extract(epoch FROM e.started_at) / 300) AS bucket
        FROM events e
        JOIN bounds b ON e.started_at >= b.s AND e.started_at < b.e
        WHERE e.started_at >= (SELECT min(s) FROM bounds)
          AND e.started_at <  (SELECT max(e) FROM bounds)
    ),
    evagg AS (
        SELECT d,
               count(*) AS events,
               count(*) FILTER (WHERE kind <> 'ai_work') AS mine,
               count(*) FILTER (WHERE kind = 'ai_work') AS ai_work,
               min(started_at) AS first_at,
               max(started_at) AS last_at
        FROM evday GROUP BY d
    ),
    buck AS (
        SELECT d,
               count(DISTINCT bucket) FILTER (WHERE kind <> 'ai_work') AS active_b,
               count(DISTINCT bucket) FILTER (WHERE kind = 'ai_work') AS ai_b
        FROM evday GROUP BY d
    ),
    sess AS (
        SELECT b.d,
               sum(greatest(0.0, extract(epoch FROM
                   (least(coalesce(ws.ended_at, now()), b.e)
                    - greatest(ws.started_at, b.s))) / 60.0)) AS minutes
        FROM work_sessions ws
        JOIN bounds b
          ON ws.started_at < b.e
         AND coalesce(ws.ended_at, now()) > b.s
         AND ws.status <> 'cancelled'
        GROUP BY b.d
    )
    SELECT to_char(b.d, 'YYYY-MM-DD') AS date,
           coalesce(a.events, 0) AS events,
           coalesce(a.mine, 0) AS mine,
           coalesce(a.ai_work, 0) AS ai_work,
           coalesce(k.active_b, 0) * 5 AS active_minutes,
           coalesce(k.ai_b, 0) * 5 AS ai_active_minutes,
           round(coalesce(s.minutes, 0.0), 2)::float8 AS session_minutes,
           a.first_at, a.last_at
    FROM bounds b
    LEFT JOIN evagg a ON a.d = b.d
    LEFT JOIN buck k ON k.d = b.d
    LEFT JOIN sess s ON s.d = b.d
    ORDER BY b.d
    """
)


@app.get("/v1/stats/daily", response_model=DailyStatsResponse)
def get_daily_stats(
    from_: date = Query(alias="from"),
    to: date = Query(...),
    tz: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    tz_name = tz or config.TZ_DEFAULT
    try:
        ZoneInfo(tz_name)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"unknown tz: {tz_name}") from e
    n_days = (to - from_).days + 1
    if n_days < 1:
        raise HTTPException(status_code=422, detail="from must be <= to")
    if n_days > 400:
        raise HTTPException(status_code=422, detail="range too large (max 400 days)")
    rows = db.execute(
        _DAILY_STATS_SQL, {"tz": tz_name, "from_d": from_, "to_d": to}
    ).mappings().fetchall()
    days = [DailyStat.model_validate(r) for r in rows]
    return DailyStatsResponse(tz=tz_name, days=days)


# --- Importers (P3-A): agent-memory records -> ai_work events ---


@app.get("/v1/importers", response_model=ImportersPage)
def list_importers(db: Session = Depends(get_db)):
    rows = db.scalars(select(ImporterState).order_by(ImporterState.name)).fetchall()
    out = [ImporterOut.model_validate(r) for r in rows]
    for o in out:
        if o.name == am_import.IMPORTER_NAME:
            o.enabled = config.AM_IMPORT_ENABLED and bool(config.AM_BASE_URL)
    return ImportersPage(importers=out)


@app.post("/v1/importers/agent-memory/run", response_model=ImporterRunOut)
def run_agent_memory_import():
    try:
        stats = am_import.import_once()
    except Exception as e:  # noqa: BLE001 - surface to the caller; the
        # background worker keeps retrying regardless (fail-soft).
        raise HTTPException(
            status_code=502, detail=f"agent-memory import failed: {e}"
        ) from e
    return ImporterRunOut(ok=True, **stats)


# Management web UI for phone/iPad (static files, tailnet-only like the API).
from pathlib import Path  # noqa: E402

from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_UI_DIR = Path(__file__).parent / "ui"
if _UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse("/ui/")
