"""Space and chat-room enumeration, with hard exclusion of direct messages."""

from __future__ import annotations

import logging
from typing import Any, Iterator

from circle_leads.config.settings import CommunityPermission
from circle_leads.scraper.pagination import CircleClient, paginate

logger = logging.getLogger(__name__)

# Chat rooms are collected ONLY when their kind is on this allowlist.
# The Headless `/messages` endpoint returns direct messages by default and
# offers no server-side filter, so this must be an allowlist: an unrecognized
# future room kind must fail closed rather than leak DMs into the pipeline.
ALLOWED_CHAT_ROOM_KINDS = {"group_chat"}
DIRECT_MESSAGE_KINDS = {"direct", "direct_message", "dm"}


def list_spaces_admin(client: CircleClient, *, per_page: int = 100) -> list[dict]:
    """List spaces via Admin API v2."""
    return list(paginate(client, "/api/admin/v2/spaces", per_page=per_page))


def list_spaces_member(client: CircleClient) -> list[dict]:
    """List member-visible spaces via the Headless Member API.

    This endpoint takes no parameters and is not paginated.
    """
    payload = client.get("/api/headless/v1/spaces")
    if isinstance(payload, list):
        return payload
    return payload.get("records") or payload.get("spaces") or []


def is_direct_message_room(room: dict[str, Any]) -> bool:
    """True when a chat room is (or may be) a direct message."""
    kind = str(room.get("chat_room_kind") or room.get("kind") or "").lower().strip()
    if kind in DIRECT_MESSAGE_KINDS:
        return True
    uuid = str(room.get("uuid") or room.get("id") or "").lower()
    if uuid.startswith("direct-"):
        return True
    # Fail closed: anything not positively identified as an allowed kind is
    # treated as a DM.
    return kind not in ALLOWED_CHAT_ROOM_KINDS


def list_group_chat_rooms(
    client: CircleClient, *, per_page: int = 100
) -> list[dict]:
    """List chat rooms, excluding every direct message.

    Filtering happens before any message-fetch call, so DM content is never
    requested at all -- not requested and discarded.
    """
    rooms = list(paginate(client, "/api/headless/v1/messages", per_page=per_page))
    allowed, excluded = [], 0
    for room in rooms:
        if is_direct_message_room(room):
            excluded += 1
            continue
        allowed.append(room)
    if excluded:
        logger.info("Excluded %d direct-message room(s) from collection", excluded)
    return allowed


def approved_spaces(
    spaces: list[dict], perm: CommunityPermission
) -> list[dict]:
    """Narrow discovered spaces to the ones the operator named.

    An empty allowlist collects nothing. Approval is opt-in per space.
    """
    if not perm.allowed_space_ids:
        logger.warning(
            "Community '%s' has no allowed_space_ids; collecting nothing.",
            perm.community_id,
        )
        return []
    allowed = {str(s) for s in perm.allowed_space_ids}
    out = []
    for sp in spaces:
        sid, slug = str(sp.get("id", "")), str(sp.get("slug", ""))
        if sid in allowed or slug in allowed:
            out.append(sp)
    return out


def approved_chat_rooms(
    rooms: list[dict], perm: CommunityPermission
) -> list[dict]:
    """Narrow chat rooms to operator-approved UUIDs, DMs already removed."""
    if not perm.allowed_chat_room_uuids:
        return []
    allowed = {str(u) for u in perm.allowed_chat_room_uuids}
    return [
        r
        for r in rooms
        if str(r.get("uuid") or r.get("id")) in allowed
        and not is_direct_message_room(r)
    ]
