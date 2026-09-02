"""Fetch comments on posts. Comments often carry the actual hiring request."""

from __future__ import annotations

import logging
from typing import Iterator

from circle_leads.scraper.normalize import normalize_comment
from circle_leads.scraper.pagination import ApiError, CircleClient, paginate

logger = logging.getLogger(__name__)


def fetch_comments_admin(
    client: CircleClient,
    post_id: str,
    *,
    per_page: int = 100,
    max_pages: int | None = None,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> Iterator[dict]:
    try:
        for record in paginate(
            client,
            "/api/admin/v2/comments",
            {"post_id": post_id},
            per_page=per_page,
            max_pages=max_pages,
        ):
            yield normalize_comment(
                record,
                post_id=post_id,
                community_url=community_url,
                permission_reference=permission_reference,
                excluded_content=excluded_content,
            )
    except ApiError as exc:
        if exc.status == 404:
            return
        raise


def fetch_comments_member(
    client: CircleClient,
    post_id: str,
    *,
    per_page: int = 100,
    max_pages: int | None = None,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> Iterator[dict]:
    path = f"/api/headless/v1/posts/{post_id}/comments"
    try:
        for record in paginate(
            client, path, per_page=per_page, max_pages=max_pages
        ):
            yield normalize_comment(
                record,
                post_id=post_id,
                community_url=community_url,
                permission_reference=permission_reference,
                excluded_content=excluded_content,
            )
    except ApiError as exc:
        if exc.status == 404:
            return
        raise
