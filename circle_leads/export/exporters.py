"""Export leads to CSV and JSON. The DB itself is the SQLite/Postgres export."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from circle_leads.storage.models import Community, Lead, Post, Space

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
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Search stored leads with the filters the CLI exposes."""
    stmt = (
        select(Lead, Post, Community, Space)
        .join(Post, Lead.post_id == Post.id)
        .join(Community, Post.community_id == Community.id)
        .outerjoin(Space, Post.space_id == Space.id)
        .where(Lead.classification == "LEAD")
        .where(Lead.lead_score >= min_score)
        .order_by(Lead.lead_score.desc(), Post.published_at.desc())
    )
    if community:
        stmt = stmt.where(Community.slug == community)
    if priority:
        stmt = stmt.where(Lead.priority == priority.upper())
    if exclude_duplicates:
        stmt = stmt.where(Lead.duplicate_of_id.is_(None))
    if limit:
        stmt = stmt.limit(limit)

    rows: list[dict[str, Any]] = []
    wanted_skills = [s.strip().lower() for s in (skills or []) if s.strip()]

    for lead, post, comm, space in session.execute(stmt).all():
        lead_skills = [s.lower() for s in (lead.skills or [])]
        if wanted_skills and not any(s in lead_skills for s in wanted_skills):
            continue
        if role:
            title = (lead.job_title or "").lower()
            if role.strip().lower() not in title:
                continue

        rows.append(
            {
                "community": comm.slug,
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
                "url": post.url,
            }
        )
    return rows


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
