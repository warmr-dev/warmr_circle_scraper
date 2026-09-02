"""Fetch messages from operator-approved group chat rooms only.

Direct messages are excluded upstream in ``community_scraper``; this module
re-checks rather than trusting its caller, because a DM leak here would be a
consent violation rather than a bug.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from circle_leads.scraper.community_scraper import is_direct_message_room
from circle_leads.scraper.normalize import normalize_chat_message, parse_timestamp
from circle_leads.scraper.pagination import CircleClient, paginate_chat_messages

logger = logging.getLogger(__name__)


def fetch_chat_messages(
    client: CircleClient,
    room: dict,
    *,
    since: datetime | None = None,
    batch: int = 100,
    max_batches: int = 20,
    community_url: str | None = None,
    permission_reference: str | None = None,
    excluded_content: list[str] | None = None,
) -> Iterator[dict]:
    if is_direct_message_room(room):
        logger.warning("Refusing to read chat room that is not an allowed group chat")
        return

    uuid = str(room.get("uuid") or room.get("id"))
    for record in paginate_chat_messages(
        client, uuid, batch=batch, max_batches=max_batches
    ):
        created = parse_timestamp(record.get("created_at"))
        if since and created and created <= since:
            return
        yield normalize_chat_message(
            record,
            chat_room_uuid=uuid,
            community_url=community_url,
            permission_reference=permission_reference,
            excluded_content=excluded_content,
        )
