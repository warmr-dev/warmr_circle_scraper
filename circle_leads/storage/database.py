"""Database session management and idempotent upserts."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from sqlalchemy import create_engine, make_url, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from circle_leads.storage.models import (
    Author,
    Base,
    Community,
    Lead,
    Post,
    ScrapeRun,
    Space,
    utcnow,
)

DEFAULT_DB_PATH = "data/circle_leads.db"


def _sanitize_db_url(url: str) -> str:
    """Clean common malformed database URLs from hosting env vars.

    - Strip surrounding whitespace/quotes and stray newlines (a pasted env var
      often carries them).
    - Fix an EMPTY port: "host:/db" or "host:?..." -> "host/db". This happens
      when a URL is built from parts and the PORT variable is unset (e.g.
      "...@$HOST:$PORT/db" with $PORT blank), which SQLAlchemy 2.x rejects with
      "invalid literal for int()" while parsing the port.
    """
    url = url.strip().strip('"').strip("'").replace("\n", "").replace("\r", "")
    # Empty port between host and the path/query/end: the colon with no digits.
    url = re.sub(r"(@[^/:?#]+):(?=[/?#]|$)", r"\1", url)
    return url


def _normalize_db_url(url: str) -> str:
    """Driver prefix + Supabase pooler port. Session-mode pooler (port 5432
    on ``*.pooler.supabase.com``) allows only ~15 clients in total — one
    SQLAlchemy default pool fills that alone, and a Render restart leaves
    the old sessions held until they time out. Transaction mode (6543) is
    the pooler meant for app servers.
    """
    url = _sanitize_db_url(url)
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - leave a still-unparseable URL alone
        return url
    host = parsed.host or ""
    if "pooler.supabase.com" in host and (parsed.port is None or parsed.port == 5432):
        parsed = parsed.set(port=6543)
        url = parsed.render_as_string(hide_password=False)
    return url


def _engine_kwargs(url: str) -> dict:
    """Keep the Postgres pool tiny so we stay under Supabase/Render limits."""
    if url.startswith("sqlite"):
        return {"future": True}
    kwargs: dict = {
        "future": True,
        "pool_pre_ping": True,
        "pool_recycle": 300,
        # Default SQLAlchemy is pool_size=5 + max_overflow=10 = 15 connections,
        # which is exactly the Supabase session-pooler cap (EMAXCONNSESSION).
        "pool_size": 2,
        "max_overflow": 1,
        # Fail fast rather than hang: a request should not wait 30s for a pooled
        # connection (that makes a misconfigured DB look like a frozen page on
        # Vercel). 5s is plenty when the DB is healthy.
        "pool_timeout": 5,
    }
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001
        parsed = None
    host = (parsed.host or "") if parsed is not None else ""
    port = parsed.port if parsed is not None else None
    # A short TCP connect timeout so an unreachable DB errors in seconds, not
    # after psycopg's long default -- the difference between "clear error" and
    # "page hangs forever" on serverless.
    connect_args: dict = {"connect_timeout": 8}
    if "pooler.supabase.com" in host or port == 6543:
        # Transaction-mode PgBouncer cannot use prepared statements.
        connect_args["prepare_threshold"] = None
    kwargs["connect_args"] = connect_args
    return kwargs


class Database:
    def __init__(self, url: str | None = None):
        if url is None:
            # No CIRCLE_LEADS_DB configured -> local SQLite. This fails on a
            # read-only serverless filesystem (Vercel: /var/task, Errno 30).
            # Rather than 500 the whole app on every request, fall back to a
            # writable temp dir so it boots -- but that DB is per-instance and
            # ephemeral, so make the misconfiguration loud instead of silent.
            try:
                Path(DEFAULT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
                url = f"sqlite:///{DEFAULT_DB_PATH}"
            except OSError:
                import tempfile
                fallback = Path(tempfile.gettempdir()) / "circle_leads.db"
                logging.getLogger(__name__).error(
                    "CIRCLE_LEADS_DB is not set and the default path %r is not "
                    "writable (read-only filesystem). Falling back to the "
                    "EPHEMERAL %s -- data will not persist. Set CIRCLE_LEADS_DB "
                    "to a Postgres URL (e.g. Supabase) for real deployments.",
                    DEFAULT_DB_PATH, fallback,
                )
                url = f"sqlite:///{fallback}"
        elif url.startswith("sqlite:///"):
            p = Path(url.replace("sqlite:///", "", 1))
            if p.parent and str(p.parent) not in ("", "."):
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass  # dir may already exist or be unwritable; engine will surface it
        else:
            url = _normalize_db_url(url)
        self.url = url
        self.engine = create_engine(url, **_engine_kwargs(url))
        self._sessionmaker = sessionmaker(bind=self.engine, future=True)
        import threading as _threading
        self._shared = _threading.local()  # holds an optional shared session

        # Creating/altering schema on every cold start opens a connection and
        # runs DDL per instance -- the connection storm that trips the pooler.
        # Skip it when told the schema is already provisioned (SKIP_DB_INIT=true,
        # the right setting for serverless once the tables exist).
        if os.environ.get("SKIP_DB_INIT", "").lower() != "true":
            Base.metadata.create_all(self.engine)
            self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Add columns introduced after a table was first created.

        create_all() makes missing tables but never alters an existing one, so
        a new field on an old DB (SQLite or Postgres) needs a tiny migration.
        Each entry is idempotent: added only if the column isn't already there.
        """
        from sqlalchemy import inspect as _inspect, text as _text

        # (table, column, DDL type + default) -- keep in sync with the models.
        additions = [
            ("communities", "watching", "BOOLEAN DEFAULT FALSE"),
            ("communities", "platform", "VARCHAR(32)"),
            ("communities", "join_type", "VARCHAR(32)"),
            ("communities", "join_type_checked_at", "TIMESTAMP"),
            ("leads", "external_synced_at", "TIMESTAMP"),
            ("circle_connections", "priority", "VARCHAR(16) DEFAULT 'normal'"),
            ("circle_connections", "notes", "TEXT"),
            # P20: bulk directory discovery + ICP filter + join-bot lifecycle.
            ("communities", "external_directory_id", "VARCHAR(64)"),
            ("communities", "directory_goals", "JSON"),
            ("communities", "directory_synced_at", "TIMESTAMP"),
            ("communities", "icp_score", "FLOAT DEFAULT 0.0"),
            ("communities", "icp_flag", "BOOLEAN DEFAULT FALSE"),
            ("communities", "icp_reasons", "JSON"),
            ("communities", "icp_checked_at", "TIMESTAMP"),
            ("communities", "icp_decided_by", "VARCHAR(32)"),
            ("communities", "join_status", "VARCHAR(32) DEFAULT 'not_attempted'"),
            ("communities", "join_status_detail", "TEXT"),
            ("communities", "join_attempted_at", "TIMESTAMP"),
            ("communities", "joined_at", "TIMESTAMP"),
            ("communities", "join_attempts", "INTEGER DEFAULT 0"),
            ("replay_sessions", "source", "VARCHAR(16) DEFAULT 'extension'"),
            # Why join_type landed where it did -- "unknown" alone doesn't say
            # whether a host is dead, timed out, or returned something odd;
            # fetch_join_classification always computes this detail, it just
            # wasn't persisted before.
            ("communities", "join_type_detail", "TEXT"),
            # P25: one host = one community. Backfilled by the manual migration
            # on Postgres; here it only has to exist so writes do not fail.
            ("communities", "host", "VARCHAR(255)"),
        ]
        # replay_sessions is a whole new table (Version B experiment); create_all
        # handles it, so no per-column entry is needed here.
        try:
            insp = _inspect(self.engine)
            existing_tables = set(insp.get_table_names())
        except Exception:  # noqa: BLE001 - not a real/inspectable engine; skip
            return
        with self.engine.begin() as conn:
            for table, column, ddl in additions:
                if table not in existing_tables:
                    continue
                cols = {c["name"] for c in insp.get_columns(table)}
                if column in cols:
                    continue
                try:
                    conn.execute(_text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                except Exception:  # noqa: BLE001 - a racing add or dialect quirk
                    pass

    @contextmanager
    def session(self) -> Iterator[Session]:
        # If a shared session is active on this thread (see shared_session),
        # reuse it so a burst of session() calls in one operation shares ONE
        # connection instead of checking out a new one each time -- a big win
        # over a network pooler (Supabase) where each checkout costs a round
        # trip. The shared owner commits once at the end.
        shared = getattr(self._shared, "session", None)
        if shared is not None:
            # Commit each block, keep the connection: sharing exists to save
            # pooler checkouts, not to hold one transaction open. With an LLM
            # call per post, one transaction spanning a whole space ran for
            # minutes, and when the pooler dropped the connection the space's
            # work was lost with it (awithub, 2026-09-19).
            try:
                yield shared
                shared.commit()
            except Exception:
                shared.rollback()
                raise
            return
        s = self._sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    @contextmanager
    def shared_session(self) -> Iterator[Session]:
        """Open one session that every nested ``session()`` call reuses, on
        this thread. Commits once on exit. Use it around a loop that would
        otherwise open a session per iteration."""
        s = self._sessionmaker()
        self._shared.session = s
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            self._shared.session = None
            s.close()


def content_hash(text: str) -> str:
    """Stable hash of normalized content, for exact-duplicate detection."""
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def simhash(text: str, bits: int = 64) -> str:
    """Locality-sensitive hash for near-duplicate detection.

    Token-shingle SimHash: near-identical texts land within a small Hamming
    distance of each other, which catches reposts and light edits that a plain
    content hash would miss.
    """
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if not tokens:
        return "0" * (bits // 4)
    shingles = (
        [" ".join(tokens[i : i + 3]) for i in range(len(tokens) - 2)]
        if len(tokens) >= 3
        else tokens
    )
    vector = [0] * bits
    for sh in shingles:
        h = int(hashlib.md5(sh.encode()).hexdigest(), 16)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    value = 0
    for i in range(bits):
        if vector[i] > 0:
            value |= 1 << i
    return f"{value:0{bits // 4}x}"


def hamming_distance(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 999
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# Circle's own directory pages: many unrelated communities are listed under
# `discover.circle.so/products/<slug>`, so the host there says nothing about
# which community a row is.
DIRECTORY_HOSTS = frozenset({"discover.circle.so", "circle.so", "www.circle.so"})


def community_host(url: str) -> str | None:
    """The host that identifies a community, or None when the host cannot."""
    host = urlsplit(url).netloc.lower()
    if not host or host in DIRECTORY_HOSTS:
        return None
    return host


def get_or_create_community(session: Session, *, slug: str, url: str, **kw) -> Community:
    # Match on slug OR url OR host: the same community can be referenced by
    # different slugs (a search find vs. a manual read) and by different urls
    # (a directory link with an invitation token vs. the bare address), but one
    # host is one community.
    host = community_host(url)
    conditions = (Community.slug == slug) | (Community.url == url)
    if host:
        conditions = conditions | (Community.host == host)
    c = session.scalar(select(Community).where(conditions))
    if c is None:
        c = Community(slug=slug, url=url, host=host, **kw)
        session.add(c)
        session.flush()
    elif host and not c.host:
        # Row written before the column existed.
        c.host = host
    return c


def get_or_create_space(
    session: Session, *, community_id: int, source_space_id: str, **kw
) -> Space:
    sp = session.scalar(
        select(Space).where(
            Space.community_id == community_id,
            Space.source_space_id == str(source_space_id),
        )
    )
    if sp is None:
        sp = Space(community_id=community_id, source_space_id=str(source_space_id), **kw)
        session.add(sp)
        session.flush()
    else:
        for k, v in kw.items():
            if v is not None:
                setattr(sp, k, v)
    return sp


def get_or_create_author(
    session: Session, *, community_id: int, source_author_id: str | None, **kw
) -> Author | None:
    if not source_author_id and not kw.get("display_name"):
        return None
    if source_author_id:
        a = session.scalar(
            select(Author).where(
                Author.community_id == community_id,
                Author.source_author_id == str(source_author_id),
            )
        )
        if a:
            return a
    elif kw.get("display_name"):
        # Readers that only know a display name (every triage/harvest/cookie
        # read) used to create a fresh row per post per read: prod had 54,579
        # authors for 2,256 distinct names on 2026-09-19. Reuse the row that
        # name already has in this community.
        # A profile URL, when the reader has one, tells two same-named people
        # apart, so it has to match as well.
        same = [
            Author.community_id == community_id,
            Author.source_author_id.is_(None),
            Author.display_name == kw["display_name"],
        ]
        if kw.get("profile_url"):
            same.append(Author.profile_url == kw["profile_url"])
        a = session.scalar(select(Author).where(*same).order_by(Author.id).limit(1))
        if a:
            return a
    a = Author(
        community_id=community_id,
        source_author_id=str(source_author_id) if source_author_id else None,
        **kw,
    )
    session.add(a)
    session.flush()
    return a


def live_original(lead: Lead | None) -> bool:
    """Whether a lead can stand as the original its near-copies point at.

    Still a LEAD, or already sent to Vini (production has that content, so a
    repost must not go out again). A demoted lead that was never sent is
    neither: filing a repost as its duplicate would hide the repost for good.
    """
    return lead is not None and (
        lead.classification == "LEAD" or lead.external_synced_at is not None
    )


def retire_lead(session: Session, lead: Lead, *, reason: str,
                decided_by: str | None = None) -> str:
    """Take back a lead that a re-classification says is not one.

    Deleted only while nothing depends on it. It is kept, demoted to NOT_LEAD
    (every LEAD count drops it) with the new reason, when production (Vini)
    already received it -- the row is the only record of what was sent --
    when an operator already reviewed it, or when other leads are filed as its
    duplicates (leads.duplicate_of_id has no ON DELETE, so deleting it would
    fail the whole read). Returns "deleted" or "demoted".
    """
    referenced = session.scalar(
        select(Lead.id).where(Lead.duplicate_of_id == lead.id).limit(1)
    )
    untouched = (lead.external_synced_at is None
                 and (lead.review_status or "pending_review") == "pending_review"
                 and referenced is None)
    if untouched:
        session.delete(lead)
        return "deleted"
    why = ("after it was sent to Vini" if lead.external_synced_at is not None
           else "after review" if (lead.review_status or "pending_review") != "pending_review"
           else "while other leads are filed as its duplicates")
    lead.classification = "NOT_LEAD"
    lead.reason = f"Re-judged not a lead {why}: {reason}"[:2000]
    if decided_by:
        lead.decided_by = decided_by
    logging.getLogger(__name__).info("Lead %s demoted, not deleted (%s)", lead.id, why)
    return "demoted"


# Circle's list responses carry a ~255-char preview (truncated_content) next to
# the whole post (tiptap_body). Every post stored before 2026-09-18 is that
# preview, and a read's identity is a hash of the text, so reading the same
# post in full would store it a second time. A preview ends in an ellipsis,
# often mid-word, and its line breaks differ from the full text's.
_PREVIEW_TAIL = re.compile(r"(?:\.{3}|…)$")
# Characters left off the end of a preview before comparing: the cut can land
# inside a word or an HTML entity.
_PREVIEW_SLACK = 12


def _stored_match(old: str | None, new: str | None) -> str | None:
    """How a stored text relates to a newly read one of the same post.

    "same" when they differ only in whitespace (two readers used to join
    TipTap nodes differently), "preview" when the stored one is a truncated
    preview of the new one, None when they are different texts.
    """
    o = _PREVIEW_TAIL.sub("", "".join((old or "").split()))
    n = "".join((new or "").split())
    if o and o == n:
        return "same"
    if len(o) < 40 or len(o) >= len(n):
        return None
    return "preview" if n.startswith(o[: len(o) - _PREVIEW_SLACK]) else None


def _is_preview_of(old: str | None, new: str | None) -> bool:
    """True when ``old`` is a truncated preview of ``new``, whitespace aside."""
    return _stored_match(old, new) == "preview"


def _stored_copy_of(
    session: Session, *, community_id: int, ctype: str,
    published_at: datetime | None, text: str,
) -> tuple[Post | None, str | None]:
    """The row holding an earlier copy of this post, and how it relates.

    Matched on the post's exact Circle timestamp (readers take it from
    created_at, to the millisecond), then on the text. Undated posts are never
    matched: nothing ties them to a stored row but the text itself.
    """
    if not isinstance(published_at, datetime):
        return None, None
    if published_at.tzinfo is not None:
        published_at = published_at.astimezone(timezone.utc).replace(tzinfo=None)
    rows = session.scalars(
        select(Post).where(
            Post.community_id == community_id,
            Post.content_type == ctype,
            Post.published_at == published_at,
        ).order_by(Post.id)
    ).all()
    for row in rows:
        how = _stored_match(row.content, text)
        if how:
            return row, how
    return None, None


def _stored_preview_of(session: Session, **kw) -> Post | None:
    row, how = _stored_copy_of(session, **kw)
    return row if how == "preview" else None


def upsert_post(session: Session, *, community_id: int, record: dict) -> tuple[Post, str]:
    """Insert or update one content item.

    Returns (post, outcome) where outcome is 'new', 'updated', or 'unchanged'.
    Identity is (community, source_content_id, content_type), so re-running a
    scrape never duplicates rows.
    """
    source_id = str(record["source_content_id"])
    ctype = record.get("content_type", "post")
    text = record.get("content") or ""
    new_hash = content_hash(text)

    existing = session.scalar(
        select(Post).where(
            Post.community_id == community_id,
            Post.source_content_id == source_id,
            Post.content_type == ctype,
        )
    )
    if existing is None:
        preview, how = _stored_copy_of(
            session, community_id=community_id, ctype=ctype,
            published_at=record.get("published_at"), text=text,
        )
        if preview is not None and how == "same":
            # Same text, other whitespace: take over the new identity, keep
            # the verdict -- there is nothing new to classify.
            preview.source_content_id = source_id
            preview.content = text
            preview.dedup_hash = new_hash
            preview.simhash = simhash(text)
            preview.url = record.get("url") or preview.url
            session.flush()
            return preview, "unchanged"
        if preview is not None:
            # Same post, stored earlier as Circle's preview. Upgrade that row
            # to the full text under the new identity (so the next read finds
            # it directly) and re-classify it; its lead, if any, stays on it.
            # Not an edit by the author, so edited_at is left alone.
            preview.source_content_id = source_id
            preview.content = text
            preview.title = record.get("title") or preview.title
            preview.url = record.get("url") or preview.url
            preview.dedup_hash = new_hash
            preview.simhash = simhash(text)
            preview.scraped_at = utcnow()
            preview.classified = False
            session.flush()
            return preview, "updated"

    if existing is not None:
        if existing.dedup_hash == new_hash:
            # Content unchanged, but backfill a better URL/permalink if we now
            # have one (e.g. a real thread link where before we had none).
            new_url = record.get("url")
            if new_url and new_url != existing.url:
                existing.url = new_url
            return existing, "unchanged"
        # Content changed since last run: refresh and re-classify.
        existing.content = text
        existing.title = record.get("title") or existing.title
        existing.dedup_hash = new_hash
        existing.simhash = simhash(text)
        existing.edited_at = record.get("edited_at") or utcnow()
        existing.scraped_at = utcnow()
        existing.classified = False
        session.flush()
        return existing, "updated"

    post = Post(
        community_id=community_id,
        space_id=record.get("space_id"),
        author_id=record.get("author_id"),
        source_content_id=source_id,
        content_type=ctype,
        thread_id=record.get("thread_id"),
        title=record.get("title"),
        content=text,
        url=record.get("url"),
        published_at=record.get("published_at"),
        dedup_hash=new_hash,
        simhash=simhash(text),
        permission_reference=record.get("permission_reference"),
    )
    session.add(post)
    try:
        session.flush()
    except IntegrityError:
        # Another process (e.g. the harvest worker while the dashboard button
        # also runs) inserted this exact post first. Roll back to it -- the
        # unique key guarantees no duplicate -- and report it as already seen.
        session.rollback()
        existing = session.scalar(
            select(Post).where(
                Post.community_id == community_id,
                Post.source_content_id == source_id,
                Post.content_type == ctype,
            )
        )
        if existing is not None:
            return existing, "unchanged"
        raise  # a different integrity problem; don't swallow it
    return post, "new"


# SimHash distance grows faster on short texts, where a single added word
# shifts a large share of the shingles. Scale the tolerance with length so a
# short repost is still caught without matching unrelated short posts.
NEAR_DUPLICATE_THRESHOLD_SHORT = 12
NEAR_DUPLICATE_THRESHOLD_LONG = 6
SHORT_TEXT_TOKENS = 40


def near_duplicate_threshold(text: str) -> int:
    tokens = len(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return (
        NEAR_DUPLICATE_THRESHOLD_SHORT
        if tokens < SHORT_TEXT_TOKENS
        else NEAR_DUPLICATE_THRESHOLD_LONG
    )


def find_near_duplicate(
    session: Session, post: Post, threshold: int | None = None
) -> Post | None:
    """Find an earlier post whose content is near-identical to this one."""
    if not post.simhash:
        return None
    # Scoped to one community: each community's data is consented to
    # separately, so a duplicate link must never cross that boundary.
    exact = session.scalar(
        select(Post)
        .where(
            Post.community_id == post.community_id,
            Post.dedup_hash == post.dedup_hash,
            Post.id < post.id,
        )
        .order_by(Post.id)
    )
    if exact:
        return exact

    if threshold is None:
        threshold = near_duplicate_threshold(post.content)

    # Select ids and hashes only -- hydrating every row as an ORM entity made
    # classification quadratic in table size.
    rows = session.execute(
        select(Post.id, Post.simhash).where(
            Post.community_id == post.community_id,
            # Earlier posts only, as the docstring always said: the oldest copy
            # is the original. With "any other post", re-judging an original
            # after its repost filed each lead as the other's duplicate, and
            # both vanished from the lead list.
            Post.id < post.id,
            Post.simhash.is_not(None),
        )
        .order_by(Post.id)
    ).all()
    for other_id, other_simhash in rows:
        if hamming_distance(post.simhash, other_simhash) <= threshold:
            return session.get(Post, other_id)
    return None


def purge_expired(session: Session, retention_days: int) -> int:
    """Delete content past its retention window, keeping outcome statistics.

    Retention is a consent commitment, not housekeeping: operators were told
    text is kept for a bounded period.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=retention_days
    )
    stale = session.scalars(select(Post).where(Post.scraped_at < cutoff)).all()
    removed = 0
    for p in stale:
        if p.lead is not None:
            session.delete(p.lead)
        session.delete(p)
        removed += 1
    return removed


def purge_community(session: Session, slug: str) -> int:
    """Operator kill switch: delete a community's stored content entirely."""
    c = session.scalar(select(Community).where(Community.slug == slug))
    if c is None:
        return 0
    posts = session.scalars(select(Post).where(Post.community_id == c.id)).all()
    removed = 0
    for p in posts:
        if p.lead is not None:
            session.delete(p.lead)
        session.delete(p)
        removed += 1
    for sp in session.scalars(select(Space).where(Space.community_id == c.id)).all():
        session.delete(sp)
    for a in session.scalars(select(Author).where(Author.community_id == c.id)).all():
        session.delete(a)
    for r in session.scalars(
        select(ScrapeRun).where(ScrapeRun.community_id == c.id)
    ).all():
        r.community_id = None
    c.permission_status = "revoked"
    # Clear the watermark: after a purge, a later re-approval must re-collect
    # from scratch rather than silently skipping the deleted window.
    c.last_synced_at = None
    return removed
