"""Blog post generation (P5): a day's material -> LLM -> blog_posts rows.

Two article kinds for the user's personal blog (generated right after the
daily report):
- summary: one readable article (800-1600 chars, plain だ・である style,
  2-4 ## headings) about what was tackled, what progressed and what is
  left over, built from the same material as the daily report (declared
  sessions + own activity/screen_summary + ai_work, superseded hidden).
- learning: up to 3 articles a day of reusable technical knowledge from
  that day's ai_work records whose memory type is one of solution/cause/
  experiment/result/issue plus the user's screen_summary. Fixed section
  structure 背景 / 問題・症状 / 原因 / 対処 / 確かめ方 / 参考, the last one
  listing the source record times and ids (memory ids, commit ids).

Every prompt says "write nothing that is not in the material", and
secret-looking strings are masked before storing (regex post-processing:
bearer values, key=value pairs, long key-shaped tokens; pure hex/dash
tokens such as uuids and git shas are kept so the 参考 section can cite
memory ids and commit ids). LLM_PROVIDER=fake returns short deterministic
Markdown (E2E). The nightly scheduler calls run_auto right after each
daily-report slot (23:55 same day / 06:00 force-regeneration of
yesterday); failures are logged only (fail-soft).

Post identity is deterministic: id = uuid5(date+type+topic) and slug =
<date>-summary | <date>-learning-<topic>, so POST /v1/blog/generate is
idempotent; force=true regenerates the summary in place and replaces the
day's learning posts. The summary post's `date` column is the link key the
reader uses to jump to that day's daily report in the management UI.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date as date_type
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal
from .daily_report import (
    ReportError,
    _block_messages,
    _chunk_blocks,
    _day_window,
    _event_line,
    _llm_chat,
    _sessions_text,
    collect_material,
)
from .models import BlogPost, Event

log = logging.getLogger("sync.blog")


def _chat(model: str, messages: list[dict]) -> str:
    """LLM call via the daily-report transport; ReportError -> BlogError."""
    try:
        return _llm_chat(model, messages)
    except ReportError as e:
        raise BlogError(str(e)) from e

BLOG_TYPES = ("summary", "learning")
# ai_work memory types that count as learning material.
LEARNING_MEMORY_TYPES = ("solution", "cause", "experiment", "result", "issue")
MAX_LEARNING_POSTS = 3
MAX_TAGS = 5
TITLE_MAX = 120
EXCERPT_MAX = 200
TOPIC_MAX = 8
# Per-record material caps (learning needs more detail than the report).
LEARN_AI_CHARS = 800
LEARN_SS_CHARS = 300
LEARN_MAX_RECORDS = 60


class BlogError(Exception):
    pass


def blog_id_for(day: date_type, type_: str, topic: str) -> uuid.UUID:
    """Deterministic post id: uuid5 of date+type+topic (idempotent upserts)."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"minecontext:sync-server:blog-post:{day.isoformat()}:{type_}:{topic}",
    )


def slug_for(day: date_type, type_: str, topic: str) -> str:
    if type_ == "summary":
        return f"{day.isoformat()}-summary"
    return f"{day.isoformat()}-learning-{topic}"


# --- secret masking (post-processing on everything we store) ---

# Bearer <token> -> Bearer ***
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
# key=value / key: value pairs (password=..., api_key: "...")
_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[-_]?key|access[-_]?key)\b"
    r"(\s*[=:]\s*)([\"'])?([^\s\"',;]{4,})"
)
# Long key-shaped tokens. Pure hex/dash strings (uuids, git shas) survive so
# the 参考 section can cite memory ids and commit ids.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9+/_=-])")
_HEXISH_RE = re.compile(r"^[0-9a-fA-F-]+$")


def mask_secrets(text: str) -> str:
    text = _BEARER_RE.sub("Bearer ***", text)

    def _kv(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{m.group(3) or ''}***"

    text = _KV_RE.sub(_kv, text)

    def _tok(m: re.Match) -> str:
        return m.group(0) if _HEXISH_RE.match(m.group(0)) else "***"

    return _TOKEN_RE.sub(_tok, text)


# --- LLM output helpers ---


def _parse_json_object(content: str) -> dict:
    """Best-effort JSON extraction (the prompt asks for JSON only)."""
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise BlogError("LLM returned no JSON object")
    try:
        data = json.loads(s[start : end + 1])
    except json.JSONDecodeError as e:
        raise BlogError(f"LLM returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise BlogError("LLM JSON is not an object")
    return data


def _norm_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = " ".join(str(t).split())
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def _norm_title(title, fallback: str) -> str:
    t = " ".join(str(title or "").split())[:TITLE_MAX]
    return t or fallback


def _fallback_excerpt(markdown: str) -> str:
    plain = re.sub(r"^#+\s*", "", markdown, flags=re.MULTILINE).strip()
    plain = " ".join(plain.split())
    return plain[:EXCERPT_MAX]


def _norm_excerpt(excerpt, markdown: str) -> str:
    e = " ".join(str(excerpt or "").split())[:EXCERPT_MAX]
    return e or _fallback_excerpt(markdown)


def _norm_markdown(markdown) -> str:
    md = str(markdown or "").strip()
    lines = md.splitlines()
    # strip accidental fences and a duplicated H1 (title is a column)
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    md = "\n".join(lines).strip()
    if not md:
        raise BlogError("LLM returned empty markdown")
    return md


def _topic_slug(topic, idx: int) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(topic or "").lower())[:TOPIC_MAX]
    return s or f"t{idx + 1}"


def _upsert_post(
    db: Session,
    *,
    day: date_type,
    type_: str,
    topic: str,
    title: str,
    excerpt: str,
    markdown: str,
    tags: list[str],
    source_event_ids: list[uuid.UUID],
    model: str,
) -> BlogPost:
    values = dict(
        id=blog_id_for(day, type_, topic),
        date=day,
        type=type_,
        slug=slug_for(day, type_, topic),
        title=mask_secrets(title),
        excerpt=mask_secrets(excerpt),
        markdown=mask_secrets(markdown),
        tags=tags,
        source_event_ids=source_event_ids,
        model=model,
    )
    stmt = pg_insert(BlogPost).values(**values)
    set_ = {k: v for k, v in values.items() if k != "id"}
    set_["updated_at"] = func.now()
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=set_)
    db.execute(stmt)
    db.commit()
    return db.get(BlogPost, values["id"])


# --- summary (one article a day) ---


def _summary_messages(day: date_type, tz_name: str, sessions_text: str, events_part: str):
    system = (
        "あなたはPC作業ログから個人のブログ記事を書く助手です。次の規則を厳守すること:\n"
        "- 材料に無いことは書かない。推測・創作・一般論を混ぜない。\n"
        "- 文体は「だ・である」調。「です・ます」は使わない。\n"
        "- 本文は800〜1600字。見出し(## )を2〜4個。H1(# )は書かない。\n"
        "- 出力は JSON オブジェクトのみ。前置き・後書き・コードブロックは書かない。"
    )
    user = (
        f"日付: {day.isoformat()}（時刻は {tz_name} の24時間表記 (HH:MM) に統一）\n\n"
        "## 材料1: 宣言された作業（work_sessions）\n"
        f"{sessions_text}\n\n"
        "## 材料2: この日の記録（時系列。activity/screen_summary=自分の作業、"
        "ai_work=AI の作業）\n"
        f"{events_part}\n\n"
        "以上の材料だけから、何に取り組み、何が進み、何が残ったかを伝える"
        "読み物の記事を1本書くこと。次の JSON を返すこと:\n"
        '{"title": "記事タイトル（40字まで）", "excerpt": "リード文（120字程度）",'
        ' "tags": ["プロジェクト名や技術名（最大5個）"], "markdown": "本文（見出し2〜4個）"}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _fake_summary(day: date_type, tz_name: str, counts: dict) -> dict:
    return {
        "title": f"まとめ {day.isoformat()}（fake）",
        "excerpt": (
            f"fake: sessions={counts['sessions']} activity={counts['activity']} "
            f"screen_summary={counts['screen_summary']} ai_work={counts['ai_work']}"
        )[:EXCERPT_MAX],
        "tags": ["fake"],
        "markdown": (
            f"## 概要（fake）\n"
            f"tz: {tz_name} / LLM_PROVIDER=fake（材料の件数のみを並べる）。\n\n"
            f"- sessions: {counts['sessions']}\n"
            f"- activity: {counts['activity']}\n"
            f"- screen_summary: {counts['screen_summary']}\n"
            f"- ai_work: {counts['ai_work']}\n"
        ),
    }


def generate_summary_post(
    db: Session, day: date_type, tz_name: str, zone: ZoneInfo, *, force: bool = False
) -> tuple[BlogPost, bool]:
    """Generate (or return the existing) summary post for `day`."""
    slug = slug_for(day, "summary", "")
    existing = db.scalars(select(BlogPost).where(BlogPost.slug == slug)).first()
    if existing is not None and not force:
        return existing, False

    sessions, events, counts, window = collect_material(db, day, zone)
    now = datetime.now(timezone.utc)
    source_ids = [e.id for e in events]
    if config.LLM_PROVIDER == "fake":
        data = _fake_summary(day, tz_name, counts)
        model = "fake"
    elif config.LLM_PROVIDER == "openai_compat":
        sessions_text = _sessions_text(sessions, zone, window, now)
        events_part = (
            "\n".join(_event_line(e, zone) for e in events) or "（この日の記録は無い）"
        )
        if len(events_part) > config.REPORT_CHUNK_CHARS:
            # same long-material strategy as the daily report: per-block
            # flash pre-summary, then integrate
            parts = []
            for label, lines in _chunk_blocks(events, zone):
                summary = _chat(
                    config.REPORT_LLM_FLASH_MODEL,
                    _block_messages(day, label, "\n".join(lines)),
                )
                parts.append(f"### {label}\n{summary.strip()}")
            events_part = "（長いため時間帯ごとの要約）\n" + "\n\n".join(parts)
        data = _parse_json_object(
            _chat(
                config.REPORT_LLM_MODEL,
                _summary_messages(day, tz_name, sessions_text, events_part),
            )
        )
        model = config.REPORT_LLM_MODEL
    else:
        raise BlogError(f"unknown LLM_PROVIDER: {config.LLM_PROVIDER}")

    markdown = _norm_markdown(data.get("markdown"))
    return (
        _upsert_post(
            db,
            day=day,
            type_="summary",
            topic="",
            title=_norm_title(data.get("title"), f"まとめ {day.isoformat()}"),
            excerpt=_norm_excerpt(data.get("excerpt"), markdown),
            markdown=markdown,
            tags=_norm_tags(data.get("tags")),
            source_event_ids=source_ids,
            model=model,
        ),
        True,
    )


# --- learning (topic-wise, up to 3 a day) ---


def collect_learning_material(
    db: Session, day: date_type, zone: ZoneInfo
) -> tuple[list[Event], list[Event], tuple[datetime, datetime]]:
    """Learning material: qualifying ai_work + the user's screen_summary.

    Returns (ai_events, screen_events, window); superseded ai_work hidden
    (same rule as the timeline default).
    """
    start_local, end_local = _day_window(day, zone)
    window = (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )
    ai_events = db.scalars(
        select(Event)
        .where(
            Event.started_at >= window[0],
            Event.started_at <= window[1],
            Event.kind == "ai_work",
            Event.meta["memory_type"].astext.in_(LEARNING_MEMORY_TYPES),
            # hide superseded ai_work (same rule as the timeline default)
            Event.meta["superseded_by"].astext.is_(None),
        )
        .order_by(Event.started_at.asc(), Event.id.asc())
    ).fetchall()
    screen_events = db.scalars(
        select(Event)
        .where(
            Event.started_at >= window[0],
            Event.started_at <= window[1],
            Event.kind == "screen_summary",
        )
        .order_by(Event.started_at.asc(), Event.id.asc())
    ).fetchall()
    return ai_events, screen_events, window


def _learning_ai_line(e: Event, zone: ZoneInfo) -> str:
    t = e.started_at.astimezone(zone).strftime("%H:%M")
    agent = (e.device_id or "?").removeprefix("agent:")
    mtype = (e.meta or {}).get("memory_type") or "?"
    tags = ",".join(str(x) for x in (e.meta or {}).get("tags") or [])
    refs = ",".join(str(x) for x in (e.meta or {}).get("refs") or [])
    parts = [f"- [{t}] (ai_work/{agent} type={mtype}) {e.title or 'ai_work'}"]
    body = (e.text or "")[:LEARN_AI_CHARS]
    line = f"{parts[0]}: {body}"
    if tags:
        line += f" [tags: {tags}]"
    if refs:
        line += f" [refs: {refs}]"
    return line + f" [id: {e.id}]"


def _learning_ss_line(e: Event, zone: ZoneInfo) -> str:
    t = e.started_at.astimezone(zone).strftime("%H:%M")
    head = " ".join(x for x in (e.app, e.title) if x)
    prefix = f" ({e.device_id})" + (f" {head}:" if head else "")
    return f"- [{t}]{prefix} {(e.text or '')[:LEARN_SS_CHARS]}"


def _learning_messages(day: date_type, tz_name: str, material: str):
    system = (
        "あなたはPC作業ログから再利用できる技術的知見をブログ記事にする助手です。"
        "次の規則を厳守すること:\n"
        "- 材料に無いことは書かない。推測・創作・一般論を混ぜない。\n"
        "- 手順・コマンド・設定値は材料にあるものだけを書く。\n"
        "- 各記事の構成は「## 背景」「## 問題・症状」「## 原因」「## 対処」"
        "「## 確かめ方」「## 参考」の6見出しをこの順ですべて使う。H1(# )は書かない。\n"
        "- 「## 参考」には根拠となった記録の時刻 (HH:MM) と id（memory id・"
        "コミットID等、材料の [id: ...]）を並べる。\n"
        "- 出力は JSON オブジェクトのみ。前置き・後書き・コードブロックは書かない。"
    )
    user = (
        f"日付: {day.isoformat()}（時刻は {tz_name} の24時間表記 (HH:MM) に統一）\n\n"
        "## 材料: この日の記録（ai_work=AI の作業で type が solution/cause/"
        "experiment/result/issue のもの、screen_summary=自分の画面記録）\n"
        f"{material}\n\n"
        "以上の材料だけから、再利用できる技術的な学びを話題ごとに記事化すること"
        f"（最大{MAX_LEARNING_POSTS}記事。材料が薄い話題は無理に作らない。"
        "学びが無ければ空配列）。各記事の topic は内容を表す半角英数字3〜8字の"
        "スラッグ。次の JSON を返すこと:\n"
        '{"articles": [{"topic": "slug", "title": "記事タイトル（40字まで）", '
        '"excerpt": "リード文（120字程度）", "tags": ["技術名等（最大5個）"], '
        '"markdown": "本文（6見出し構成）"}]}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _fake_learning_articles(
    day: date_type, zone: ZoneInfo, ai_events: list[Event], screen_events: list[Event]
) -> list[dict]:
    """Deterministic grouping by the first tag (E2E)."""
    groups: dict[str, list[Event]] = {}
    for e in ai_events:
        tags = [str(t) for t in (e.meta or {}).get("tags") or [] if str(t)]
        key = tags[0] if tags else "untagged"
        groups.setdefault(key, []).append(e)
    articles = []
    for idx, (tag, evs) in enumerate(list(groups.items())[:MAX_LEARNING_POSTS]):
        ref_lines = [
            f"- [{e.started_at.astimezone(zone).strftime('%H:%M')}] {e.title or 'ai_work'} [id: {e.id}]"
            for e in evs
        ]
        markdown = (
            f"## 背景（fake）\n{day.isoformat()} の ai_work（tags: {tag}）"
            f"{len(evs)}件・screen_summary {len(screen_events)}件。\n\n"
            "## 問題・症状\nfake 出力のため省略。\n\n"
            "## 原因\nfake 出力のため省略。\n\n"
            "## 対処\nfake 出力のため省略。\n\n"
            "## 確かめ方\nfake 出力のため省略。\n\n"
            "## 参考\n" + "\n".join(ref_lines) + "\n"
        )
        articles.append(
            {
                "topic": f"t{idx + 1}",
                "title": f"学び {tag}（fake）",
                "excerpt": f"fake: {tag} の ai_work {len(evs)}件",
                "tags": [tag, "fake"],
                "markdown": markdown,
            }
        )
    return articles


def generate_learning_posts(
    db: Session, day: date_type, tz_name: str, zone: ZoneInfo, *, force: bool = False
) -> tuple[list[BlogPost], bool]:
    """Generate (or return the existing) learning posts for `day`.

    force=true replaces the day's learning posts (delete + regenerate);
    without force, existing posts are returned as-is (idempotent).
    """
    existing = db.scalars(
        select(BlogPost)
        .where(BlogPost.date == day, BlogPost.type == "learning")
        .order_by(BlogPost.created_at.asc(), BlogPost.id.asc())
    ).fetchall()
    if existing and not force:
        return existing, False

    ai_events, screen_events, _window = collect_learning_material(db, day, zone)
    source_ids = [e.id for e in ai_events] + [e.id for e in screen_events]
    if config.LLM_PROVIDER == "fake":
        articles = _fake_learning_articles(day, zone, ai_events, screen_events)
        model = "fake"
    elif config.LLM_PROVIDER == "openai_compat":
        if not ai_events and not screen_events:
            articles = []
        else:
            lines = [_learning_ai_line(e, zone) for e in ai_events]
            lines += [_learning_ss_line(e, zone) for e in screen_events]
            if len(lines) > LEARN_MAX_RECORDS:
                lines = lines[:LEARN_MAX_RECORDS]
                lines.append("（このほかの記録は省略）")
            data = _parse_json_object(
                _chat(
                    config.REPORT_LLM_MODEL,
                    _learning_messages(day, tz_name, "\n".join(lines) or "（材料なし）"),
                )
            )
            articles = data.get("articles") or []
            if not isinstance(articles, list):
                raise BlogError("LLM JSON 'articles' is not a list")
        model = config.REPORT_LLM_MODEL
    else:
        raise BlogError(f"unknown LLM_PROVIDER: {config.LLM_PROVIDER}")

    posts: list[BlogPost] = []
    if existing:  # force: replace the day's learning set
        for old in existing:
            db.delete(old)
        db.commit()
    seen_slugs: set[str] = set()
    for idx, art in enumerate(articles[:MAX_LEARNING_POSTS]):
        topic = _topic_slug(art.get("topic"), idx)
        slug = slug_for(day, "learning", topic)
        while slug in seen_slugs:  # two topics sanitized to the same slug
            topic = f"{topic}x{len(seen_slugs)}"
            slug = slug_for(day, "learning", topic)
        seen_slugs.add(slug)
        markdown = _norm_markdown(art.get("markdown"))
        posts.append(
            _upsert_post(
                db,
                day=day,
                type_="learning",
                topic=topic,
                title=_norm_title(art.get("title"), f"学び {day.isoformat()}"),
                excerpt=_norm_excerpt(art.get("excerpt"), markdown),
                markdown=markdown,
                tags=_norm_tags(art.get("tags")),
                source_event_ids=source_ids,
                model=model,
            )
        )
    return posts, True


# --- entry points (API + scheduler) ---


def generate_blog(
    db: Session, day: date_type, tz_name: str, types: list[str], *, force: bool = False
) -> dict:
    """Generate the requested blog types for `day`; returns the API payload."""
    zone = ZoneInfo(tz_name)
    out: dict = {"date": day.isoformat(), "tz": tz_name}
    if "summary" in types:
        post, generated = generate_summary_post(db, day, tz_name, zone, force=force)
        out["summary"] = {"post": post, "generated": generated}
    else:
        out["summary"] = None
    if "learning" in types:
        posts, generated = generate_learning_posts(db, day, tz_name, zone, force=force)
        out["learning"] = [
            {"post": p, "generated": generated} for p in posts
        ]
    else:
        out["learning"] = []
    return out


def run_auto(day: date_type, tz_name: str, force: bool) -> bool:
    """One scheduled generation (both types); failures are logged only."""
    try:
        with SessionLocal() as db:
            out = generate_blog(db, day, tz_name, list(BLOG_TYPES), force=force)
            n = (1 if out["summary"] else 0) + len(out["learning"])
            return n > 0
    except Exception as e:  # noqa: BLE001 - log and retry at the next slot
        log.warning("blog auto-generation for %s failed: %s", day, e)
        return False
