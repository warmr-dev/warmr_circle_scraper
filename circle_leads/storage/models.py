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
    BigInteger,
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
    # The host from ``url``, kept as its own column because ``url`` alone does
    # not identify a community: the same place arrives once as a directory link
    # (``/join?invitation_token=...``) and once bare, and the two spellings used
    # to become two rows that both collected the same posts.
    host: Mapped[str | None] = mapped_column(String(255), index=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    discovery_source: Mapped[str | None] = mapped_column(String(255))

    # Which platform hosts this community: "circle" (subdomain or custom domain,
    # readable by the harvest), "discover" (a discover.circle.so listing whose
    # real host isn't resolved yet), "circle_infra", "facebook", "slack",
    # "skool", "mighty_networks", "other", ... NULL on rows created before this
    # column existed -- the harvest falls back to a URL check for those.
    platform: Mapped[str | None] = mapped_column(String(32), index=True)

    # How this community can be joined: "free_join", "paid", "invite_only",
    # "locked_unknown", or "unknown" -- see discovery/join_type.py. Refreshed
    # on every harvest read, independent of `platform`/`access_status`: a
    # community can be free_join and already have public spaces, or paid and
    # still expose a public board. NULL until the harvest's first read.
    join_type: Mapped[str | None] = mapped_column(String(32), index=True)
    join_type_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Why -- e.g. "request failed: ConnectionError", "HTTP 404", "is_private=true".
    # Distinguishes a dead host from a real 401 from a weird response, all of
    # which otherwise collapse into the same "unknown" bucket.
    join_type_detail: Mapped[str | None] = mapped_column(Text)

    # Provenance from the Circle discovery-directory crawl (discovery/circle_directory.py):
    # the directory's own id (for dedup/incremental refresh) and which goal
    # categories it was listed under. NULL for rows found by other means.
    external_directory_id: Mapped[str | None] = mapped_column(String(64), index=True)
    directory_goals: Mapped[list | None] = mapped_column(JSON, default=list)
    directory_synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ICP (ideal-customer-profile) fit for a software development company's lead
    # gen -- see classifier/icp_relevance.py. Deliberately separate from
    # relevance_score/relevant below (the older web-search ranking heuristic):
    # the two are not yet proven to agree, so don't collapse them prematurely.
    icp_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    icp_flag: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    icp_reasons: Mapped[list | None] = mapped_column(JSON, default=list)
    icp_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    icp_decided_by: Mapped[str | None] = mapped_column(String(32))

    # Outcome of the auto-join bot's attempt (circle_leads/join/), independent of
    # `join_type` above: join_type is a property of the community (how it CAN be
    # joined), join_status is what OUR bot actually did about it.
    join_status: Mapped[str] = mapped_column(
        String(32), default="not_attempted", index=True
    )
    join_status_detail: Mapped[str | None] = mapped_column(Text)
    join_attempted_at: Mapped[datetime | None] = mapped_column(DateTime)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime)
    join_attempts: Mapped[int] = mapped_column(Integer, default=0)

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
    # What the last harvest read actually saw, set with last_synced_at:
    # "public" (space list answered), "private" (401/403 on the list or on
    # every space), "gone" (404, or the host no longer maps to a community),
    # "error" (network failure, 429, 5xx -- says nothing about the community).
    # Decides how soon the harvest reads it again (harvest._recheck_hours).
    # NULL on rows last read before this column existed.
    read_outcome: Mapped[str | None] = mapped_column(String(32))
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


class JoinStatus(str, enum.Enum):
    """Outcome of the auto-join bot's attempt on one community (circle_leads/join/).

    Separate from `Community.join_type` (how a community CAN be joined, refreshed
    on every harvest read): this tracks what OUR bot actually did about it.
    """

    NOT_ATTEMPTED = "not_attempted"
    QUEUED = "queued"
    PENDING_APPROVAL = "pending_approval"
    JOINED = "joined"
    PAID_SKIP = "paid_skip"
    INVITE_SKIP = "invite_skip"
    SUBSCRIPTION_EXPIRED_SKIP = "subscription_expired_skip"
    FAILED = "failed"
    # The host (or the custom domain it redirects to) no longer resolves --
    # nothing to join. Recorded so the queue stops spending visits on it.
    DEAD_HOST = "dead_host"
    # Circle made us a member but the new-member "Create a profile" step is
    # still open: until it is saved every API call answers 400 "Please confirm
    # before proceeding", so nothing can be read. Stays in the join queue so
    # the bot comes back to finish it.
    PROFILE_PENDING = "profile_pending"
    # Signing in goes through the community's own website (ecommerce, ulule),
    # not Circle: it needs an account there, which is a person's decision. The
    # bot never types the Circle login into such a page.
    EXTERNAL_LOGIN = "external_login"
    # The join itself is possible, but the community's form asks something the
    # bot has no honest answer for -- icecampus wants a passport number and a
    # date of birth. A person decides; the queue must stop spending visits on
    # it in the meantime.
    NEEDS_HUMAN = "needs_human"


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

    # Where this session came from: "extension" (Chrome cookie-capture
    # extension, the original path) or "join_bot" (circle_leads/join/, minted
    # directly on the server by the auto-join bot). Same table, same encryption,
    # same scanning.py consumption -- this is purely for observability.
    source: Mapped[str] = mapped_column(String(16), default="extension")

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
    # "scan" (one host) | "scan_all" | "harvest" | "join" (circle_leads/join/,
    # host = the community to auto-join; claimed only by the VPS join-worker,
    # never by Railway's worker -- see storage/job_queue.py::claim_next)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    host: Mapped[str | None] = mapped_column(String(255))   # for kind=scan|join
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


class WatchMode(str, enum.Enum):
    """How a community's feed is read, or why it is not read at all."""

    ANON = "anon"        # the feed answers without a session
    COOKIE = "cookie"    # needs the stored session
    OFF = "off"          # closed, dead, or moved: checked once a day at most


class WatchState(Base):
    """Where the fast poller has got to in one community's feed.

    The harvest reads everything every few hours; this reads one page of
    ``/internal_api/home_page_posts?sort=latest`` every couple of minutes and
    passes anything new to the same triage the harvest uses. It keeps a
    watermark rather than a timestamp: a post is new when its id is one we have
    not seen, which survives clock skew, a slow write, and a post edited after
    publication. ``recent_ids`` exists because ``last_post_id`` alone is not
    enough -- Circle can publish an id below the newest one when a draft is
    released or a post is restored.
    """

    __tablename__ = "watch_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    community_id: Mapped[int] = mapped_column(
        ForeignKey("communities.id"), unique=True, index=True
    )
    host: Mapped[str] = mapped_column(String(255), index=True)

    mode: Mapped[str] = mapped_column(String(16), default=WatchMode.ANON.value, index=True)
    # The busy communities are polled at the fast interval; one that has been
    # silent for weeks is polled slowly until it posts again, which is what
    # keeps the whole watch list inside one IP's request budget.
    tier: Mapped[str] = mapped_column(String(16), default="fast", index=True)

    last_post_id: Mapped[int | None] = mapped_column(BigInteger)
    recent_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    etag: Mapped[str | None] = mapped_column(String(255))

    next_check_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_new_at: Mapped[datetime | None] = mapped_column(DateTime)

    # "ok" | "not_modified" | "unauthorized" | "ratelimited" | "challenge" |
    # "notfound" | "error" -- the same words the egress probe uses.
    last_status: Mapped[str | None] = mapped_column(String(32), index=True)
    last_detail: Mapped[str | None] = mapped_column(Text)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)

    posts_seen: Mapped[int] = mapped_column(Integer, default=0)
    leads_found: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class JoinFormQuestion(Base):
    """A question some community's join/profile form asked, seen by the bot.

    One row per distinct question, keyed by its field type plus normalised
    label, so the same "Company" field on fifty communities is one row with
    ``times_seen=50``. Choices are per community and kept only as the last
    example -- the answer is matched against each form's own options at fill
    time. See circle_leads/join/forms.py.
    """

    __tablename__ = "join_form_questions"
    __table_args__ = (UniqueConstraint("question_key", name="uq_join_form_question_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    question_key: Mapped[str] = mapped_column(String(300), index=True)
    label: Mapped[str] = mapped_column(Text)
    field_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    example_choices: Mapped[list | None] = mapped_column(JSON, default=list)
    # Set when a differently-worded question was judged to mean the same thing
    # as an earlier one; its answers are then shared with that one.
    same_as_id: Mapped[int | None] = mapped_column(ForeignKey("join_form_questions.id"))
    times_seen: Mapped[int] = mapped_column(Integer, default=0)
    first_host: Mapped[str | None] = mapped_column(String(255))
    last_host: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class JoinFormAnswer(Base):
    """The answer one account gives to one question -- reused on every form
    that asks it. ``source`` says who wrote it (the persona file, the LLM, or a
    person); a person can correct any row and later fills use the correction."""

    __tablename__ = "join_form_answers"
    __table_args__ = (
        UniqueConstraint("question_id", "account", name="uq_join_form_answer_question_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("join_form_questions.id"), index=True)
    account: Mapped[str] = mapped_column(String(32), index=True)
    answer: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))  # persona | ai | human
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class JoinFormFill(Base):
    """What the bot put into which community's form, as which account --
    the audit trail for "what did we tell this community about ourselves"."""

    __tablename__ = "join_form_fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    community_id: Mapped[int | None] = mapped_column(ForeignKey("communities.id"), index=True)
    host: Mapped[str | None] = mapped_column(String(255))
    account: Mapped[str] = mapped_column(String(32))
    question_id: Mapped[int | None] = mapped_column(ForeignKey("join_form_questions.id"))
    label: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    # answered | needs_human (no answer the bot may give)
    outcome: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
