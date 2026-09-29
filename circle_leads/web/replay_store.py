"""At-rest storage for member session cookies, keyed by community host.

Cookie blobs are encrypted with AES-GCM when ``CIRCLE_CRED_KEY`` is set. If it
is not set, cookies are stored in PLAINTEXT (a plain-JSON blob, prefixed so we
know how to read it back). Plaintext is the user's explicit choice: it removes
the setup step, at the cost that the raw session cookie is readable by anyone
with database access. Set CIRCLE_CRED_KEY to encrypt at rest instead.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, update

from circle_leads.connector.credentials import decrypt, encrypt
from circle_leads.storage.database import Database
from circle_leads.storage.models import (
    CircleConnection, Community, ConnectionState, ReplaySession,
)

logger = logging.getLogger(__name__)

# Marks a blob stored without encryption, so load knows not to decrypt.
_PLAINTEXT_PREFIX = "plain:"


class ReplayKeyMissing(RuntimeError):
    """Encrypting stored sessions was asked for with no CIRCLE_CRED_KEY set.
    Storing never raises it: without a key a session is stored as plaintext."""


def _key() -> str | None:
    return os.environ.get("CIRCLE_CRED_KEY") or None


def _serialize(cookies: list[dict]) -> str:
    """Encrypt if a key is set, else store a marked plaintext blob."""
    payload = json.dumps(cookies)
    key = _key()
    if key:
        return encrypt(payload, key)
    return _PLAINTEXT_PREFIX + payload


def _deserialize(blob: str) -> list[dict]:
    if blob.startswith(_PLAINTEXT_PREFIX):
        return json.loads(blob[len(_PLAINTEXT_PREFIX):])
    key = _key()
    if not key:
        # Encrypted blob but no key now -> unreadable; treat as empty.
        return []
    return json.loads(decrypt(blob, key))


def store_session(db: Database, host: str, cookies: list[dict],
                  *, member_label: str | None = None) -> dict:
    """Upsert a session for ``host`` (encrypted if a key is set, else plaintext).

    ``member_label`` is the account the cookies belong to. A caller that does
    not know it -- the Chrome extension, a pasted cookie -- keeps the one on
    record: it used to be wiped on every refresh. With none on record, the
    account the community was joined with is the best guess there is.
    """
    blob = _serialize(cookies)
    with db.session() as s:
        row = s.scalar(select(ReplaySession).where(ReplaySession.host == host))
        if row is None:
            row = ReplaySession(host=host)
            s.add(row)
        row.encrypted_cookies = blob
        row.cookie_count = len(cookies)
        if member_label:
            row.member_label = member_label
        elif not row.member_label:
            row.member_label = s.scalar(
                select(Community.join_account)
                .where(Community.host == host, Community.join_account.is_not(None))
                .limit(1)
            )
        row.last_result = None
        row.last_detail = "Stored; not yet tested from the server."
        rid = row.id
    return {"host": host, "cookie_count": len(cookies)}


def connect_host(db: Database, host: str, cookies: list[dict],
                 *, member_label: str | None = None) -> dict:
    """Store a session AND make sure the host is scannable -- the two steps
    every caller needs together (the manual cookie-paste dashboard route, and
    circle_leads/join/joiner.py after an auto-join). Both scan_cookie_host()
    and cookie_hosts_vip_first() (circle_leads/scanning.py) key off a
    CircleConnection row existing; without it the worker would never pick the
    host up despite the cookie being stored."""
    result = store_session(db, host, cookies, member_label=member_label)
    with db.session() as s:
        if s.scalar(select(CircleConnection).where(CircleConnection.host == host)) is None:
            s.add(CircleConnection(host=host, state=ConnectionState.NOT_CONNECTED.value))
    _watch_now(db, host)
    return result


def _watch_now(db: Database, host: str) -> None:
    """A stored session means we are a member: read the feed every 2 minutes
    from now on. A failure here must not lose the session just stored."""
    from circle_leads.watch.poller import watch_member_now
    try:
        watch_member_now(db, host)
    except Exception:  # noqa: BLE001
        logger.exception("could not put %s on the 2-minute watch", host)


def encrypt_plaintext_sessions(db: Database) -> int:
    """Encrypt every session still stored as plaintext; returns how many.

    Run once, where CIRCLE_CRED_KEY is set, after every service that reads
    sessions has the same key: one without it reads an encrypted session as
    no session at all (_deserialize). Only the blob changes -- the account,
    the source, the last result and the timestamps stay as they were.
    """
    key = _key()
    if not key:
        raise ReplayKeyMissing("CIRCLE_CRED_KEY is not set: there is nothing to encrypt with.")
    done = 0
    with db.session() as s:
        rows = s.execute(select(ReplaySession.id, ReplaySession.host, ReplaySession.encrypted_cookies)
                         .where(ReplaySession.encrypted_cookies.like(_PLAINTEXT_PREFIX + "%"))).all()
        for row_id, host, blob in rows:
            cookies = json.loads(blob[len(_PLAINTEXT_PREFIX):])
            sealed = encrypt(json.dumps(cookies), key)
            if json.loads(decrypt(sealed, key)) != cookies:
                raise RuntimeError(f"{host}: the encrypted session does not read back; nothing written")
            s.execute(update(ReplaySession).where(ReplaySession.id == row_id).values(
                encrypted_cookies=sealed,
                # Named, so onupdate leaves it alone: the dashboard reads it as
                # the time the session was last refreshed.
                updated_at=ReplaySession.updated_at,
            ))
            done += 1
    return done


def load_cookies(db: Database, host: str) -> list[dict] | None:
    """Decrypt and return the stored cookies for ``host``, or None."""
    with db.session() as s:
        row = s.scalar(select(ReplaySession).where(ReplaySession.host == host))
        if row is None:
            return None
        blob = row.encrypted_cookies
    return _deserialize(blob)


def record_result(db: Database, host: str, result: str, detail: str) -> None:
    with db.session() as s:
        row = s.scalar(select(ReplaySession).where(ReplaySession.host == host))
        if row is not None:
            row.last_result = result
            row.last_detail = detail
            row.last_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)


def list_sessions(db: Database) -> list[dict]:
    """Non-sensitive metadata only — never the cookies themselves."""
    with db.session() as s:
        rows = s.scalars(select(ReplaySession).order_by(ReplaySession.host)).all()
        return [{
            "host": r.host, "cookie_count": r.cookie_count,
            "member_label": r.member_label,
            "last_result": r.last_result, "last_detail": r.last_detail,
            "last_attempt_at": r.last_attempt_at.isoformat() if r.last_attempt_at else None,
        } for r in rows]


def delete_session(db: Database, host: str) -> None:
    with db.session() as s:
        row = s.scalar(select(ReplaySession).where(ReplaySession.host == host))
        if row is not None:
            s.delete(row)
