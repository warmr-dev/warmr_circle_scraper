"""Normalize Circle payloads into one minimal record shape.

Deliberately minimal: display name and a stable author id, never emails,
phone numbers, bios, or cross-community identity matching. The goal is to
detect a request for help, not to build a dossier on a member.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"\b(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}\b")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = _TAG.sub(" ", value)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def redact_pii(text: str, excluded: list[str] | None = None) -> str:
    """Remove contact details the consent contract excludes from storage."""
    excluded = excluded or []
    if "email_addresses" in excluded:
        text = _EMAIL.sub("[email removed]", text)
    if "phone_numbers" in excluded:
        text = _PHONE.sub("[phone removed]", text)
    return text


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _body_text(payload: dict) -> str:
    """Pull the text body out of Circle's several body shapes."""
    body = payload.get("body")
    if isinstance(body, dict):
        for key in ("body", "plain_text", "text", "value"):
            if body.get(key):
                return strip_html(str(body[key]))
    for key in ("body_plain_text", "plain_text", "content", "text", "message", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return strip_html(value)
    if isinstance(body, str):
        return strip_html(body)
    return ""


def _author(payload: dict) -> dict[str, Any]:
    raw = (
        payload.get("user")
        or payload.get("author")
        or payload.get("community_member")
        or payload.get("sender")
        or {}
    )
    if not isinstance(raw, dict):
        raw = {}
    return {
        "source_author_id": raw.get("id") or payload.get("community_member_id"),
        "display_name": raw.get("name")
            or raw.get("display_name")
            or raw.get("full_name")
            or payload.get("user_name"),
        "profile_url": raw.get("url") or raw.get("profile_url"),
    }


def normalize_post(
    payload: dict,
    *,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize a post record."""
    title = strip_html(payload.get("name") or payload.get("title"))
    body = _body_text(payload)
    # Title carries real hiring signal ("Hiring a Flutter dev"), so classify
    # both together rather than the body alone.
    text = f"{title}\n\n{body}".strip() if title and title != body else (body or title)
    text = redact_pii(text, excluded_content)

    url = payload.get("url")
    if url and community_url and url.startswith("/"):
        url = community_url.rstrip("/") + url

    return {
        "source_content_id": str(payload.get("id")),
        "content_type": "post",
        "thread_id": str(payload.get("id")),
        "title": title or None,
        "content": text,
        "url": url,
        "published_at": parse_timestamp(
            payload.get("published_at") or payload.get("created_at")
        ),
        "space_source_id": str(payload["space_id"]) if payload.get("space_id") else None,
        "author": _author(payload),
        "permission_reference": permission_reference,
    }


def normalize_comment(
    payload: dict,
    *,
    post_id: str | None = None,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> dict[str, Any]:
    text = redact_pii(_body_text(payload), excluded_content)
    url = payload.get("url")
    if url and community_url and url.startswith("/"):
        url = community_url.rstrip("/") + url
    return {
        "source_content_id": str(payload.get("id")),
        "content_type": "comment",
        "thread_id": str(post_id or payload.get("post_id") or ""),
        "title": None,
        "content": text,
        "url": url,
        "published_at": parse_timestamp(payload.get("created_at")),
        "space_source_id": str(payload["space_id"]) if payload.get("space_id") else None,
        "author": _author(payload),
        "permission_reference": permission_reference,
    }


def normalize_chat_message(
    payload: dict,
    *,
    chat_room_uuid: str,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> dict[str, Any]:
    text = redact_pii(_body_text(payload), excluded_content)
    return {
        "source_content_id": str(payload.get("id")),
        "content_type": "chat_message",
        "thread_id": chat_room_uuid,
        "title": None,
        "content": text,
        "url": payload.get("url"),
        "published_at": parse_timestamp(payload.get("created_at")),
        "space_source_id": None,
        "author": _author(payload),
        "permission_reference": permission_reference,
    }
