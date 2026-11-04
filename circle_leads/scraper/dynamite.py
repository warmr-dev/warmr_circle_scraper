"""Read discussions from dc.dynamitecircle.com.

That host is not a Circle community. Circle's ``/internal_api`` answers 404,
and the member session lives in Firebase (project ``dc-nav``), not in
``_circle_session``. The token stays in ``settings`` under
``dynamite_circle_session``. It is never written to ``replay_sessions``: the
Circle watcher would try to send it as cookies.

The browse query is copied from the site's own ``useBrowseRooms``. A shorter
filter (``type == discussion`` ordered by ``lastActivityAt``) asks for a
composite index the project does not have and returns 400.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from circle_leads.connector.credentials import decrypt, encrypt
from circle_leads.scraper.normalize import parse_timestamp, redact_pii, strip_html
from circle_leads.storage.database import get_or_create_community
from circle_leads.storage.settings_store import (
    RETRY_AFTER_INTERRUPT_MINUTES,
    get_setting,
    record_stage_finish,
    set_setting,
)

logger = logging.getLogger(__name__)

HOST = "dc.dynamitecircle.com"
ORIGIN = f"https://{HOST}"
PROJECT = "dc-nav"
NAME = "Dynamite Circle"
PLATFORM = "dynamite"
SESSION_KEY = "dynamite_circle_session"
KEY_LAST_RUN = "dynamite_last_run"
STAGE = "dynamite"

INTERVAL = timedelta(hours=2)
DEFAULT_ROOMS = 25
DEFAULT_MESSAGES = 8
MAX_TEXT = 12_000
_PLAINTEXT_PREFIX = "plain:"
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_DOCS = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
    "/databases/(default)/documents"
)
_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"


class SessionInvalid(RuntimeError):
    """The stored Firebase refresh token was rejected. A new browser login
    has to replace it. The message never includes the token."""


def discussion_query(limit: int) -> dict:
    """The structuredQuery the site sends for the discussion list."""
    return {
        "from": [{"collectionId": "rooms"}],
        "where": {
            "compositeFilter": {
                "op": "AND",
                "filters": [
                    _eq("type", "discussion"),
                    _eq("flags.isEnabled", True),
                    _eq("flags.isArchived", False),
                    _eq("flags.isBrowsable", True),
                    _eq("flags.isDCB", False),
                ],
            }
        },
        "orderBy": [
            {"field": {"fieldPath": "flags.isPinnedGlobal"}, "direction": "DESCENDING"},
            {"field": {"fieldPath": "lastActivityAt"}, "direction": "DESCENDING"},
        ],
        "limit": int(limit),
    }


def decode_value(value: Any) -> Any:
    """One Firestore REST value (stringValue, mapValue, ...) as a Python value."""
    if not isinstance(value, dict) or not value:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "timestampValue" in value:
        return value["timestampValue"]
    if "nullValue" in value:
        return None
    if "bytesValue" in value:
        return None
    if "mapValue" in value:
        fields = (value.get("mapValue") or {}).get("fields") or {}
        return {key: decode_value(item) for key, item in fields.items()}
    if "arrayValue" in value:
        items = (value.get("arrayValue") or {}).get("values") or []
        return [decode_value(item) for item in items]
    if "referenceValue" in value:
        return value["referenceValue"]
    return None


def decode_document(doc: dict) -> tuple[str, dict]:
    name = str(doc.get("name") or "")
    fields = {
        key: decode_value(item)
        for key, item in (doc.get("fields") or {}).items()
    }
    return name.rsplit("/", 1)[-1], fields


def records_from_room(
    room_id: str,
    fields: dict,
    messages: list[tuple[str, dict]],
    *,
    excluded: list[str] | None = None,
    max_age_days: int = 0,
    now: datetime | None = None,
) -> list[dict]:
    """Opening post plus replies, in the shape ``triage_records`` consumes.

    The opening post is the room body. The message whose id is
    ``{room}_main`` (flag ``isMain``) repeats it, so that message is dropped.
    Deleted, sunk, and hidden rooms are dropped the same way the site drops
    them after the query.
    """
    flags = fields.get("flags") or {}
    if flags.get("isDeleted") or flags.get("isSunk") or flags.get("isHidden"):
        return []
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    records: list[dict] = []
    opening = _record(
        room_id=room_id,
        message_id=None,
        title=fields.get("name") or None,
        html=fields.get("body"),
        author=fields.get("authorUser") or {},
        author_id=fields.get("authorID"),
        published=fields.get("createdAt"),
        excluded=excluded,
        max_age_days=max_age_days,
        now=now,
    )
    if opening:
        records.append(opening)
    for message_id, message in messages[:DEFAULT_MESSAGES]:
        message_flags = message.get("flags") or {}
        if message_flags.get("isDeleted") or message_flags.get("isMain"):
            continue
        if message_id == f"{room_id}_main":
            continue
        reply = _record(
            room_id=room_id,
            message_id=message_id,
            title=None,
            html=message.get("source") or message.get("sourceBKP"),
            author=message.get("authorUser") or {},
            author_id=message.get("authorID"),
            published=message.get("createdAt") or message.get("sentAt"),
            excluded=excluded,
            max_age_days=max_age_days,
            now=now,
        )
        if reply:
            records.append(reply)
    return records


def save_session(db, api_key: str, refresh_token: str) -> None:
    """Store the Firebase refresh material. Encrypted when CIRCLE_CRED_KEY is set."""
    if not api_key or not refresh_token:
        raise ValueError("Dynamite Circle session is missing a key or refresh token")
    payload = json.dumps({"api_key": api_key, "refresh_token": refresh_token})
    key = os.environ.get("CIRCLE_CRED_KEY") or None
    blob = encrypt(payload, key) if key else _PLAINTEXT_PREFIX + payload
    set_setting(db, SESSION_KEY, blob)


def _seal_if_needed(db, session: dict) -> None:
    if not os.environ.get("CIRCLE_CRED_KEY"):
        return
    blob = get_setting(db, SESSION_KEY) or ""
    if blob.startswith(_PLAINTEXT_PREFIX):
        save_session(db, session["api_key"], session["refresh_token"])


def load_session(db) -> dict | None:
    """``{api_key, refresh_token}``, or None when nothing usable is stored.

    A ``plain:`` blob is readable even when a key is set now. An encrypted
    blob with no key is treated as no session, same as Circle cookies.
    """
    blob = get_setting(db, SESSION_KEY)
    if not blob:
        return None
    if blob.startswith(_PLAINTEXT_PREFIX):
        raw = blob[len(_PLAINTEXT_PREFIX):]
    else:
        key = os.environ.get("CIRCLE_CRED_KEY") or None
        if not key:
            return None
        raw = decrypt(blob, key)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    api_key = data.get("api_key")
    refresh_token = data.get("refresh_token")
    if not api_key or not refresh_token:
        return None
    return {"api_key": api_key, "refresh_token": refresh_token}


def refresh_access(http, api_key: str, refresh_token: str) -> tuple[str, str]:
    """Exchange a refresh token for an id token.

    Firebase rejects the call without the site's own Referer. The returned
    refresh token replaces the stored one when Google rotates it. Failures
    raise ``SessionInvalid`` or ``RuntimeError`` whose text does not contain
    the token or the API key (the key would otherwise leak via the request URL).
    """
    headers = {
        "Referer": ORIGIN + "/",
        "Origin": ORIGIN,
    }
    try:
        response = http.post(
            _TOKEN_URL,
            params={"key": api_key},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers=headers,
            timeout=30,
        )
    except Exception:
        raise RuntimeError("Dynamite Circle token refresh failed") from None
    if response.status_code in (400, 401, 403):
        raise SessionInvalid(
            f"Dynamite Circle session was rejected (HTTP {response.status_code})"
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Dynamite Circle token refresh failed (HTTP {response.status_code})"
        )
    try:
        body = response.json()
    except Exception:
        raise RuntimeError("Dynamite Circle token refresh failed") from None
    token = body.get("id_token") or body.get("access_token")
    if not token:
        raise SessionInvalid("Dynamite Circle session was rejected")
    return token, body.get("refresh_token") or refresh_token


def run_scheduled(
    db,
    requirements,
    *,
    use_llm: bool = False,
    force: bool = False,
    limit: int = DEFAULT_ROOMS,
    http=None,
) -> dict | None:
    """Read discussions and hand them to triage.

    Returns None when there is no session or the last read was under two
    hours ago (unless ``force``). Auth failure raises so the caller can
    record ``dynamite_last_error``. The refresh token is saved as soon as
    Google rotates it, before the read, so a later error does not strand
    the new token.
    """
    session = load_session(db)
    if session is None:
        logger.info("dynamite: no session stored")
        return None
    # A session saved where the encryption key could not be read is plaintext.
    # The next run on a machine that has CIRCLE_CRED_KEY seals it in place.
    _seal_if_needed(db, session)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not force and not _due(db, now):
        return None

    set_setting(db, KEY_LAST_RUN, now.isoformat())
    client = http or _http()
    id_token, refresh_token = refresh_access(
        client, session["api_key"], session["refresh_token"]
    )
    if refresh_token != session["refresh_token"]:
        save_session(db, session["api_key"], refresh_token)

    rooms = _fetch_rooms(client, id_token, limit)
    excluded = list(getattr(requirements, "excluded_content", None) or [])
    max_age = int(getattr(requirements, "max_post_age_days", 0) or 0)
    records: list[dict] = []
    for room_id, fields in rooms:
        try:
            messages = _fetch_messages(client, id_token, room_id)
        except SessionInvalid:
            raise
        except Exception:
            logger.warning("dynamite: replies for one discussion were not read")
            messages = []
        records.extend(records_from_room(
            room_id, fields, messages,
            excluded=excluded, max_age_days=max_age, now=now,
        ))

    _ensure_community(db)
    from circle_leads.triage.pipeline import triage_records

    triaged = triage_records(
        db, records, requirements,
        community=HOST, source_url=ORIGIN, use_llm=use_llm,
    )
    result = {
        "rooms": len(rooms),
        "records": len(records),
        "leads": len(triaged.new_leads),
        "too_old": triaged.too_old,
    }
    record_stage_finish(db, STAGE, result=result)
    logger.info(
        "dynamite: rooms=%s records=%s leads=%s",
        result["rooms"], result["records"], result["leads"],
    )
    return result


def _eq(path: str, value: str | bool) -> dict:
    if isinstance(value, bool):
        encoded: dict[str, Any] = {"booleanValue": value}
    else:
        encoded = {"stringValue": value}
    return {
        "fieldFilter": {
            "field": {"fieldPath": path},
            "op": "EQUAL",
            "value": encoded,
        }
    }


def _record(
    *,
    room_id: str,
    message_id: str | None,
    title: str | None,
    html: Any,
    author: dict,
    author_id: Any,
    published: Any,
    excluded: list[str] | None,
    max_age_days: int,
    now: datetime,
) -> dict | None:
    text = redact_pii(strip_html(html if isinstance(html, str) else ""), excluded)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rsplit(" ", 1)[0].rstrip() + "…"
    if not text:
        return None
    when = parse_timestamp(published)
    if _too_old(when, max_age_days, now):
        return None
    user_name = author.get("userName") if isinstance(author, dict) else None
    source_id = None
    if isinstance(author, dict) and author.get("userID") not in (None, ""):
        source_id = str(author.get("userID"))
    elif author_id not in (None, ""):
        source_id = str(author_id)
    display = author.get("displayName") if isinstance(author, dict) else None
    if message_id:
        url = f"{ORIGIN}/room/{room_id}/message/{message_id}/discussion"
    else:
        url = f"{ORIGIN}/room/{room_id}/discussion"
    profile = None
    if isinstance(user_name, str) and _USERNAME.match(user_name):
        profile = f"{ORIGIN}/profile/{user_name}"
    return {
        "title": title if isinstance(title, str) and title.strip() else None,
        "content": text,
        "url": url,
        "published_at": when,
        "thread_id": room_id,
        "author": {
            "display_name": display if isinstance(display, str) else None,
            "source_author_id": source_id,
            "profile_url": profile,
        },
    }


def _too_old(when: datetime | None, max_age_days: int, now: datetime) -> bool:
    if not max_age_days or when is None:
        return False
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when < now - timedelta(days=max_age_days)


def _due(db, now: datetime) -> bool:
    last = _parse(get_setting(db, KEY_LAST_RUN))
    if last is None or now - last >= INTERVAL:
        return True
    finished = _parse(get_setting(db, f"{STAGE}_last_finish"))
    interrupted = finished is None or finished < last
    if interrupted:
        return now - last >= timedelta(minutes=RETRY_AFTER_INTERRUPT_MINUTES)
    return False


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


def _ensure_community(db) -> None:
    """One community row, platform ``dynamite``.

    A null platform counts as Circle, and the harvest would then spend a
    request on a host whose Circle API 404s. ``get_or_create_community``
    only applies fields when it inserts, so an existing blank row is updated.
    """
    with db.session() as s:
        community = get_or_create_community(
            s, slug=HOST, url=ORIGIN, name=NAME,
            platform=PLATFORM, discovery_source="dynamite",
        )
        if community.platform in (None, "circle", "discover"):
            community.platform = PLATFORM
        if not community.name:
            community.name = NAME


def _http():
    import requests

    client = requests.Session()
    client.headers["User-Agent"] = "warmr-circle-scraper"
    return client


def _bearer(id_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {id_token}"}


def _fetch_rooms(http, id_token: str, limit: int) -> list[tuple[str, dict]]:
    try:
        response = http.post(
            f"{_DOCS}:runQuery",
            json={"structuredQuery": discussion_query(limit)},
            headers=_bearer(id_token),
            timeout=60,
        )
    except Exception:
        raise RuntimeError("Dynamite Circle discussion query failed") from None
    _raise_for_status(response, "discussion query")
    return [decode_document(doc) for doc in _query_documents(response)]


def _fetch_messages(http, id_token: str, room_id: str) -> list[tuple[str, dict]]:
    query = {
        "structuredQuery": {
            "from": [{"collectionId": "messages"}],
            "orderBy": [
                {"field": {"fieldPath": "createdAt"}, "direction": "DESCENDING"}
            ],
            "limit": DEFAULT_MESSAGES,
        }
    }
    try:
        response = http.post(
            f"{_DOCS}/rooms/{room_id}:runQuery",
            json=query,
            headers=_bearer(id_token),
            timeout=30,
        )
    except Exception:
        return []
    if response.status_code == 400:
        return _list_messages(http, id_token, room_id)
    if response.status_code in (401, 403):
        raise SessionInvalid(
            f"Dynamite Circle session was rejected (HTTP {response.status_code})"
        )
    if response.status_code != 200:
        return []
    return [decode_document(doc) for doc in _query_documents(response)]


def _list_messages(http, id_token: str, room_id: str) -> list[tuple[str, dict]]:
    try:
        response = http.get(
            f"{_DOCS}/rooms/{room_id}/messages",
            params={"pageSize": DEFAULT_MESSAGES},
            headers=_bearer(id_token),
            timeout=30,
        )
    except Exception:
        return []
    if response.status_code in (401, 403):
        raise SessionInvalid(
            f"Dynamite Circle session was rejected (HTTP {response.status_code})"
        )
    if response.status_code != 200:
        return []
    try:
        body = response.json()
    except Exception:
        return []
    return [decode_document(doc) for doc in (body.get("documents") or [])]


def _query_documents(response) -> list[dict]:
    try:
        body = response.json()
    except Exception:
        raise RuntimeError("Dynamite Circle returned a response that was not JSON") from None
    rows = body if isinstance(body, list) else [body]
    return [row["document"] for row in rows if isinstance(row, dict) and row.get("document")]


def _raise_for_status(response, what: str) -> None:
    if response.status_code in (401, 403):
        raise SessionInvalid(
            f"Dynamite Circle session was rejected (HTTP {response.status_code})"
        )
    if response.status_code != 200:
        raise RuntimeError(f"Dynamite Circle {what} failed (HTTP {response.status_code})")
