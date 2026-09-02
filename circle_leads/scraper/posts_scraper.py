"""Fetch posts from approved spaces, incrementally."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from circle_leads.scraper.normalize import normalize_post, parse_timestamp
from circle_leads.scraper.pagination import CircleClient, paginate

logger = logging.getLogger(__name__)


def fetch_posts_admin(
    client: CircleClient,
    space_id: str,
    *,
    since: datetime | None = None,
    per_page: int = 100,
    max_pages: int | None = None,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> Iterator[dict]:
    """Posts for one space via Admin API v2.

    Circle's list endpoint has no `created_at_gt` filter, so ``since`` is
    applied client-side: results come back newest-first, so we stop walking
    pages once we cross the watermark instead of fetching the whole history.
    """
    params = {"space_id": space_id, "status": "published", "sort": "latest"}
    for record in paginate(
        client, "/api/admin/v2/posts", params, per_page=per_page, max_pages=max_pages
    ):
        published = parse_timestamp(
            record.get("published_at") or record.get("created_at")
        )
        if since and published and published <= since:
            logger.debug("Reached watermark for space %s", space_id)
            return
        yield normalize_post(
            record,
            community_url=community_url,
            permission_reference=permission_reference,
            excluded_content=excluded_content,
        )


def fetch_posts_member(
    client: CircleClient,
    space_id: str,
    *,
    since: datetime | None = None,
    per_page: int = 100,
    max_pages: int | None = None,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> Iterator[dict]:
    """Posts for one space via the Headless Member API."""
    path = f"/api/headless/v1/spaces/{space_id}/posts"
    for record in paginate(
        client, path, {"status": "published"}, per_page=per_page, max_pages=max_pages
    ):
        published = parse_timestamp(
            record.get("published_at") or record.get("created_at")
        )
        if since and published and published <= since:
            return
        record.setdefault("space_id", space_id)
        yield normalize_post(
            record,
            community_url=community_url,
            permission_reference=permission_reference,
            excluded_content=excluded_content,
        )
