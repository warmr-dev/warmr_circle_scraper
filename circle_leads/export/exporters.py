"""Export leads to CSV and JSON. The DB itself is the SQLite/Postgres export."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from circle_leads.storage.models import Author, Community, Lead, Post, Space

CSV_COLUMNS = [
    "community",
    "author",
    "content",
    "classification",
    "confidence",
    "lead_score",
    "job_title",
    "skills",
    "published_at",
    "url",
]

EXTENDED_COLUMNS = CSV_COLUMNS + [
    "priority",
    "space",
    "employment_type",
    "hire_target",
    "company",
    "budget",
    "location",
    "urgency",
    "evidence_quote",
    "reason",
    "decided_by",
    "permission_reference",
    "is_duplicate",
]


@dataclass
class LeadRow:
    data: dict[str, Any]

    def get(self, key: str) -> Any:
        return self.data.get(key)


def query_leads(
    session: Session,
    *,
    role: str | None = None,
    skills: list[str] | None = None,
    community: str | None = None,
    min_score: int = 0,
    priority: str | None = None,
    exclude_duplicates: bool = True,
    review_status: str | None = None,
    search: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    date_field: str = "published",
    sort: str = "score",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Search stored leads with the filters the CLI and dashboard expose.

    ``search`` is a case-insensitive substring match over the post, author,
    community, space and extracted lead fields. ``since``/``until`` bound
    ``date_field``: ``"published"`` (when the post was written) or ``"found"``
    (when we filed the lead). ``sort`` is ``"score"``, ``"newest"`` (post date)
    or ``"found"``.
    """
    if date_field not in LEAD_DATE_FIELDS:
        raise ValueError(f"date_field must be one of {sorted(LEAD_DATE_FIELDS)}")
    if sort not in LEAD_SORTS:
        raise ValueError(f"sort must be one of {sorted(LEAD_SORTS)}")

    stmt = (
        select(Lead, Post, Community, Space)
        # Load the author in the same query: post.author is otherwise a lazy
        # load, i.e. one extra round trip per lead (200+ on the dashboard).
        .options(joinedload(Post.author))
        .join(Post, Lead.post_id == Post.id)
        .join(Community, Post.community_id == Community.id)
        .outerjoin(Space, Post.space_id == Space.id)
        .where(Lead.classification == "LEAD")
        .where(Lead.lead_score >= min_score)
    )
    if sort == "newest":
        stmt = stmt.order_by(Post.published_at.desc().nulls_last(), Lead.id.desc())
    elif sort == "found":
        stmt = stmt.order_by(Lead.created_at.desc(), Lead.id.desc())
    else:
        stmt = stmt.order_by(Lead.lead_score.desc(), Post.published_at.desc())

    date_column = Post.published_at if date_field == "published" else Lead.created_at
    if since:
        stmt = stmt.where(date_column >= _naive_utc(since))
    if until:
        stmt = stmt.where(date_column < _naive_utc(until))
    if search and search.strip():
        pattern = "%" + _escape_like(search.strip()) + "%"
        stmt = stmt.where(or_(
            *(column.ilike(pattern, escape="\\") for column in (
                Post.content, Post.title, Lead.job_title, Lead.company,
                Lead.location, Lead.budget, Community.slug, Community.name, Space.name,
            )),
            Post.author.has(Author.display_name.ilike(pattern, escape="\\")),
        ))
    if role and role.strip():
        stmt = stmt.where(
            Lead.job_title.ilike("%" + _escape_like(role.strip()) + "%", escape="\\")
        )
    if community:
        stmt = stmt.where(Community.slug == community)
    if priority:
        stmt = stmt.where(Lead.priority == priority.upper())
    if exclude_duplicates:
        stmt = stmt.where(Lead.duplicate_of_id.is_(None))
    if review_status:
        stmt = stmt.where(Lead.review_status == review_status)

    wanted_skills = [s.strip().lower() for s in (skills or []) if s.strip()]
    # Skills live in a JSON list and are matched here in Python, so the SQL
    # limit would cut rows before that filter; cap after it instead.
    if limit and not wanted_skills:
        stmt = stmt.limit(limit)

    rows: list[dict[str, Any]] = []
    for lead, post, comm, space in session.execute(stmt).all():
        if limit and len(rows) >= limit:
            break
        lead_skills = [s.lower() for s in (lead.skills or [])]
        if wanted_skills and not any(s in lead_skills for s in wanted_skills):
            continue

        rows.append(
            {
                "id": lead.id,
                "review_status": lead.review_status or "pending_review",
                "community": comm.slug,
                "community_name": comm.name,
                "community_url": comm.url,
                "space": space.name if space else None,
                "author": post.author.display_name if post.author else None,
                "author_profile_url": post.author.profile_url if post.author else None,
                "content": post.content,
                "content_type": post.content_type,
                "classification": lead.classification,
                "confidence": round(lead.confidence, 3),
                "lead_score": lead.lead_score,
                "priority": lead.priority,
                "job_title": lead.job_title,
                "skills": lead.skills or [],
                "employment_type": lead.employment_type,
                "hire_target": lead.hire_target,
                "company": lead.company,
                "budget": lead.budget,
                "location": lead.location,
                "urgency": lead.urgency,
                "evidence_quote": lead.evidence_quote,
                "reason": lead.reason,
                "decided_by": lead.decided_by,
                "score_breakdown": lead.score_breakdown or {},
                "permission_reference": post.permission_reference,
                "is_duplicate": lead.duplicate_of_id is not None,
                "published_at": post.published_at.isoformat() if post.published_at else None,
                "scraped_at": post.scraped_at.isoformat() if post.scraped_at else None,
                "found_at": lead.created_at.isoformat() if lead.created_at else None,
                "title": post.title,
                "url": post.url,
            }
        )
    return rows


LEAD_DATE_FIELDS = {"published", "found"}
LEAD_SORTS = {"score", "newest", "found"}


def _naive_utc(value: datetime) -> datetime:
    """Timestamps are stored as naive UTC; compare against the same."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _escape_like(text: str) -> str:
    """Match ``%`` and ``_`` typed into a search box literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Excel and Sheets execute a cell beginning with any of these. Author names and
# post bodies are written by community members, so they are untrusted here.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _escape_formula(text: str) -> str:
    """Neutralize spreadsheet formula injection without altering the reading."""
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _serialize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _csv_cell(value: Any) -> str:
    return _escape_formula(_serialize(value))


def to_csv(
    rows: Iterable[dict[str, Any]], path: str | Path, *, extended: bool = False
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    columns = EXTENDED_COLUMNS if extended else CSV_COLUMNS
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _csv_cell(row.get(c)) for c in columns})
    return out


def to_json(rows: Iterable[dict[str, Any]], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(list(rows), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return out
