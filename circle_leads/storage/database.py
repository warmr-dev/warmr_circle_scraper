"""Database session management and idempotent upserts."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, select
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


class Database:
    def __init__(self, url: str | None = None):
        if url is None:
            Path(DEFAULT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{DEFAULT_DB_PATH}"
        elif url.startswith("sqlite:///"):
            p = Path(url.replace("sqlite:///", "", 1))
            if p.parent and str(p.parent) not in ("", "."):
                p.parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        self.engine = create_engine(url, future=True)
        self._sessionmaker = sessionmaker(bind=self.engine, future=True)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
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


def get_or_create_community(session: Session, *, slug: str, url: str, **kw) -> Community:
    c = session.scalar(select(Community).where(Community.slug == slug))
    if c is None:
        c = Community(slug=slug, url=url, **kw)
        session.add(c)
        session.flush()
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
    a = Author(
        community_id=community_id,
        source_author_id=str(source_author_id) if source_author_id else None,
        **kw,
    )
    session.add(a)
    session.flush()
    return a


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

    if existing is not None:
        if existing.dedup_hash == new_hash:
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
    session.flush()
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
    exact = session.scalar(
        select(Post).where(
            Post.dedup_hash == post.dedup_hash, Post.id != post.id
        ).order_by(Post.id)
    )
    if exact:
        return exact
    if threshold is None:
        threshold = near_duplicate_threshold(post.content)
    candidates = session.scalars(
        select(Post).where(Post.id != post.id, Post.simhash.is_not(None))
    ).all()
    for c in candidates:
        if hamming_distance(post.simhash, c.simhash) <= threshold:
            return c
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
    return removed
