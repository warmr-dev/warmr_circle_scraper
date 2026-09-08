"""At-rest storage for member session cookies, keyed by community host.

Cookie blobs are encrypted with AES-GCM when ``CIRCLE_CRED_KEY`` is set. If it
is not set, cookies are stored in PLAINTEXT (a plain-JSON blob, prefixed so we
know how to read it back). Plaintext is the user's explicit choice: it removes
the setup step, at the cost that the raw session cookie is readable by anyone
with database access. Set CIRCLE_CRED_KEY to encrypt at rest instead.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import select

from circle_leads.connector.credentials import decrypt, encrypt
from circle_leads.storage.database import Database
from circle_leads.storage.models import ReplaySession

# Marks a blob stored without encryption, so load knows not to decrypt.
_PLAINTEXT_PREFIX = "plain:"


class ReplayKeyMissing(RuntimeError):
    """Kept for compatibility; no longer raised (plaintext fallback is used)."""


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
    """Upsert a session for ``host`` (encrypted if a key is set, else plaintext)."""
    blob = _serialize(cookies)
    with db.session() as s:
        row = s.scalar(select(ReplaySession).where(ReplaySession.host == host))
        if row is None:
            row = ReplaySession(host=host)
            s.add(row)
        row.encrypted_cookies = blob
        row.cookie_count = len(cookies)
        row.member_label = member_label
        row.last_result = None
        row.last_detail = "Stored; not yet tested from the server."
        rid = row.id
    return {"host": host, "cookie_count": len(cookies)}


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
