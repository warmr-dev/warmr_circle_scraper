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

    # Which platform hosts this community: "circle" (subdomain or custom domain,
    # readable by the harvest), "discover" (a discover.circle.so listing whose
    # real host isn't resolved yet), "circle_infra", "facebook", "slack",
    # "skool", "mighty_networks", "other", ... NULL on rows created before this
    # column existed -- the harvest falls back to a URL check for those.
    platform: Mapped[str | None] = mapped_column(String(32), index=True)

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

    # Watchlist: a "subscribed" community is polled first on the fast (5-min)
    # harvest lane, so its new posts surface within minutes.
    watching: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

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
    # When this lead was POSTed to the production Vini ingest endpoint.
    external_synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    post: Mapped[Post] = relationship(back_populates="lead")


class ActivityLog(Base):
    """Append-only record of what the system did and why.

    Feeds the dashboard's activity view: which community was triaged, how many
    posts were read, what the classifier decided and on what basis. Never
    stores credentials -- only what was examined and what was concluded.
    """

    __tablename__ = "activity_log"
    __table_args__ = (Index("ix_activity_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    # triage | ingest | classify | discover | export | purge | auth
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # info | success | warning | error
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)

    community: Mapped[str | None] = mapped_column(String(255), index=True)
    space: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON, default=dict)

    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    leads_found: Mapped[int] = mapped_column(Integer, default=0)
    decided_by: Mapped[str | None] = mapped_column(String(32))


class Setting(Base):
    """Key-value app settings, editable at runtime (e.g. the harvest schedule).

    Lives in the DB (not .env) so it can be changed from the dashboard without a
    redeploy -- the worker reads it each run to decide whether to harvest.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


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


class ConnectionState(str, enum.Enum):
    """Auth state of a private-community connection, driven by the local
    connector. Deliberately distinguishes 'logged in' from 'can access the
    community' from 'session expired' -- these are separately detectable and
    must not be conflated (per the connector spec)."""

    NOT_CONNECTED = "not_connected"          # host added, never authenticated
    AUTHENTICATION_REQUIRED = "authentication_required"  # needs (re)login
    AUTHENTICATING = "authenticating"        # a login window is open
    CONNECTED = "connected"                  # logged in AND community readable
    SESSION_EXPIRED = "session_expired"      # was connected, session lapsed
    ACCESS_DENIED = "access_denied"          # logged in but not a member
    ERROR = "error"                          # unexpected failure


class ConnectionPriority(str, enum.Enum):
    """How eagerly a connected private community is scanned.

    Ordering matters: the scan queue sorts by ``SCAN_ORDER`` so VIP communities
    are read first, and PAUSED ones are skipped without deleting the connection.
    """

    VIP = "vip"          # scan first, on the fast lane
    NORMAL = "normal"    # the default
    LOW = "low"          # scan last, when there's room
    PAUSED = "paused"    # keep the record, but never scan

    @classmethod
    def values(cls) -> set[str]:
        return {p.value for p in cls}


# Sort key for the scan queue: lower runs earlier. PAUSED is filtered out
# before sorting, so its rank only matters for a stable display order.
SCAN_ORDER = {
    ConnectionPriority.VIP.value: 0,
    ConnectionPriority.NORMAL.value: 1,
    ConnectionPriority.LOW.value: 2,
    ConnectionPriority.PAUSED.value: 3,
}


class Connector(Base):
    """A paired local Circle Connector (runs on the user's own computer).

    The connector holds the browser session locally; Railway only ever knows
    this record. Pairing: the dashboard mints a one-time code, the connector
    exchanges it for a long-lived token whose HASH is stored here (never the
    token itself). No Circle credentials, cookies, or profiles are stored.
    """

    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))  # e.g. "Yer's laptop"

    # One-time pairing code (short-lived); cleared once claimed.
    pairing_code: Mapped[str | None] = mapped_column(String(64), index=True)
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    paired: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # SHA-256 of the connector's API token. The token itself is shown once at
    # pairing and never persisted server-side.
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)  # heartbeat
    agent_info: Mapped[str | None] = mapped_column(String(255))  # OS/version, non-sensitive


class CircleConnection(Base):
    """A private Circle community the user connects via the local connector.

    Tracks only NON-sensitive state: the host, the auth state, and counts. The
    authenticated browser session lives exclusively in the connector's local
    Playwright profile -- never here, never on Railway.
    """

    __tablename__ = "circle_connections"
    __table_args__ = (UniqueConstraint("host", name="uq_circle_connection_host"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), index=True
    )
    # Optional link to the discovered Community row (created on first sync).
    community_id: Mapped[int | None] = mapped_column(
        ForeignKey("communities.id"), index=True
    )

    host: Mapped[str] = mapped_column(String(255), index=True)  # altea.circle.so
    name: Mapped[str | None] = mapped_column(String(512))       # display name
    member_label: Mapped[str | None] = mapped_column(String(255))  # who you're signed in as

    # How this community is treated by the scan queue. VIP communities are
    # scanned first and more often; PAUSED ones are skipped entirely without
    # losing the record (and its login) the way removing them would.
    priority: Mapped[str] = mapped_column(
        String(16), default=ConnectionPriority.NORMAL.value, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)  # your own reminder, e.g. why it matters

    state: Mapped[str] = mapped_column(
        String(32), default=ConnectionState.NOT_CONNECTED.value, index=True
    )
    state_detail: Mapped[str | None] = mapped_column(Text)

    spaces_total: Mapped[int] = mapped_column(Integer, default=0)
    spaces_readable: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class ReplaySession(Base):
    """EXPERIMENT (Version B): a stored Circle browser session for server-side
    replay from Railway.

    This exists at the user's explicit, on-record request, overriding the
    original 'no cookie export/replay' rule. It is deliberately isolated:

    - Cookie values are stored ENCRYPTED at rest (AES-GCM via CIRCLE_CRED_KEY).
      Never store the plaintext blob.
    - This is expected to be short-lived: Circle binds cf_clearance to the
      original IP/device, so a replay from Railway's IP is re-challenged
      quickly. ``last_result`` records what actually happened on each attempt.
    - Nothing here is a Circle API credential; it is a captured member session.
    """

    __tablename__ = "replay_sessions"
    __table_args__ = (UniqueConstraint("host", name="uq_replay_session_host"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(255), index=True)

    # AES-GCM ciphertext of the cookies JSON. Plaintext is never persisted.
    encrypted_cookies: Mapped[str] = mapped_column(Text)
    # Non-sensitive metadata for the dashboard.
    cookie_count: Mapped[int] = mapped_column(Integer, default=0)
    member_label: Mapped[str | None] = mapped_column(String(255))

    # Outcome of the most recent server-side replay attempt.
    last_result: Mapped[str | None] = mapped_column(String(32))   # ok|challenged|expired|error
    last_detail: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class ScanJob(Base):
    """A unit of work the Railway worker executes, enqueued by the dashboard.

    This is the control plane: Vercel (or any dashboard) just writes a row here
    and returns instantly; the always-on worker polls, claims, and runs it. No
    heavy work happens inside a web request -- so the UI stays fast and the
    worker (warm, pooled, no timeout) does the scanning.
    """

    __tablename__ = "scan_jobs"
    __table_args__ = (Index("ix_scan_jobs_pick", "state", "priority", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # "scan" (one host) | "scan_all" | "harvest"
    kind: Mapped[str] = mapped_column(String(32), index=True)
    host: Mapped[str | None] = mapped_column(String(255))   # for kind=scan
    priority: Mapped[int] = mapped_column(Integer, default=1)  # 0=VIP first

    state: Mapped[str] = mapped_column(
        String(16), default=JobState.QUEUED.value, index=True
    )
    detail: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
