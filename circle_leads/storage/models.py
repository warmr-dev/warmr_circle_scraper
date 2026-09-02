"""SQLAlchemy models.

Schema notes:
- Every community carries an explicit ``permission_status``. Nothing is
  ingested unless an operator has approved it; see ``AccessState``.
- Content rows keep ``permission_reference`` so any stored text can be traced
  back to the approval that authorized collecting it.
- ``dedup_hash`` and ``source_content_id`` support incremental, idempotent
  ingestion across repeated runs.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AccessState(str, enum.Enum):
    """Lifecycle of a community's access, per the task spec plus consent states."""

    NOT_VISITED = "not_visited"
    VISITED = "visited"
    JOINED = "joined"
    PENDING_APPROVAL = "pending_approval"
    REQUIRES_MANUAL_ACTION = "requires_manual_action"
    NOT_ACCESSIBLE = "not_accessible"


class PermissionStatus(str, enum.Enum):
    """Operator consent state. Only APPROVED communities may be ingested."""

    CANDIDATE = "candidate"
    CONTACTED = "contacted"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"


class RunState(str, enum.Enum):
    DISCOVERING = "DISCOVERING"
    ACCESSIBLE = "ACCESSIBLE"
    REQUIRES_MANUAL_ACTION = "REQUIRES_MANUAL_ACTION"
    SCRAPING = "SCRAPING"
    CLASSIFYING = "CLASSIFYING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class Classification(str, enum.Enum):
    LEAD = "LEAD"
    NOT_LEAD = "NOT_LEAD"
    UNCERTAIN = "UNCERTAIN"


class Community(Base):
    __tablename__ = "communities"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(1024), unique=True, index=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    discovery_source: Mapped[str | None] = mapped_column(String(255))

    access_status: Mapped[str] = mapped_column(
        String(32), default=AccessState.NOT_VISITED.value, index=True
    )
    permission_status: Mapped[str] = mapped_column(
        String(32), default=PermissionStatus.CANDIDATE.value, index=True
    )

    relevance_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    relevance_reasons: Mapped[list | None] = mapped_column(JSON, default=list)
    relevant: Mapped[bool] = mapped_column(Boolean, default=False)

    description: Mapped[str | None] = mapped_column(Text)
    price_label: Mapped[str | None] = mapped_column(String(64))
    operator_contact: Mapped[str | None] = mapped_column(String(512))
    approval_reference: Mapped[str | None] = mapped_column(String(512))
    ingestion_route: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    spaces: Mapped[list["Space"]] = relationship(back_populates="community")
    posts: Mapped[list["Post"]] = relationship(back_populates="community")

    @property
    def is_ingestable(self) -> bool:
        """Ingestion requires explicit, current operator approval."""
        return self.permission_status == PermissionStatus.APPROVED.value


class Space(Base):
    __tablename__ = "spaces"
    __table_args__ = (UniqueConstraint("community_id", "source_space_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("communities.id"), index=True)
    source_space_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(512))
    slug: Mapped[str | None] = mapped_column(String(255))
    space_type: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(1024))

    # An operator approves specific spaces; unapproved ones are never read.
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    community: Mapped[Community] = relationship(back_populates="spaces")


class Author(Base):
    __tablename__ = "authors"
    __table_args__ = (UniqueConstraint("community_id", "source_author_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("communities.id"), index=True)
    source_author_id: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(512))
    profile_url: Mapped[str | None] = mapped_column(String(1024))
    headline: Mapped[str | None] = mapped_column(Text)


class Post(Base):
    """A post, comment, or chat message normalized into one shape."""

    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("community_id", "source_content_id", "content_type"),
        Index("ix_posts_published_at", "published_at"),
        Index("ix_posts_dedup_hash", "dedup_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("communities.id"), index=True)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("spaces.id"), index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"), index=True)

    source_content_id: Mapped[str] = mapped_column(String(128))
    content_type: Mapped[str] = mapped_column(String(32), default="post")
    thread_id: Mapped[str | None] = mapped_column(String(128), index=True)

    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1024), index=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    dedup_hash: Mapped[str] = mapped_column(String(64))
    simhash: Mapped[str | None] = mapped_column(String(32), index=True)
    permission_reference: Mapped[str | None] = mapped_column(String(512))
    classified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    community: Mapped[Community] = relationship(back_populates="posts")
    space: Mapped[Space | None] = relationship()
    author: Mapped[Author | None] = relationship()
    lead: Mapped["Lead | None"] = relationship(back_populates="post", uselist=False)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_classification", "classification"),
        Index("ix_leads_lead_score", "lead_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id"), unique=True, index=True
    )

    classification: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)
    classifier_version: Mapped[str | None] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(32))

    # Must be an exact substring of the source text; enforced at write time.
    evidence_quote: Mapped[str | None] = mapped_column(Text)

    lead_score: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[str | None] = mapped_column(String(16), index=True)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON, default=dict)

    job_title: Mapped[str | None] = mapped_column(String(512))
    skills: Mapped[list | None] = mapped_column(JSON, default=list)
    employment_type: Mapped[str | None] = mapped_column(String(64))
    hire_target: Mapped[str | None] = mapped_column(String(64))
    company: Mapped[str | None] = mapped_column(String(512))
    budget: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    urgency: Mapped[str | None] = mapped_column(String(32))

    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id"), index=True
    )
    review_status: Mapped[str] = mapped_column(String(32), default="pending_review")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    post: Mapped[Post] = relationship(back_populates="lead")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    community_id: Mapped[int | None] = mapped_column(
        ForeignKey("communities.id"), index=True
    )
    state: Mapped[str] = mapped_column(String(32), default=RunState.DISCOVERING.value)
    route: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Watermark for incremental sync (Data API `created_at_gt` semantics).
    cursor_created_at_gt: Mapped[datetime | None] = mapped_column(DateTime)

    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    leads_found: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
