"""Encrypted-at-rest storage for replay sessions (Version B experiment).

Cookie blobs are encrypted with AES-GCM before they touch the database, using
CIRCLE_CRED_KEY. If that key is not set, storing a session is refused — we do
not persist captured Circle sessions in plaintext.

Honest limitation: the server must decrypt to replay, so it holds the key in
its own environment. This protects the DB dump, not the running server. That is
the best available given the user's choice to store the session server-side.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import select

from circle_leads.connector.credentials import decrypt, encrypt
from circle_leads.storage.database import Database
from circle_leads.storage.models import ReplaySession


class ReplayKeyMissing(RuntimeError):
    """CIRCLE_CRED_KEY is not set, so a session cannot be stored encrypted."""


def _key() -> str:
    key = os.environ.get("CIRCLE_CRED_KEY")
    if not key:
        raise ReplayKeyMissing(
            "CIRCLE_CRED_KEY is not set. Refusing to store a captured Circle "
            "session in plaintext. Set CIRCLE_CRED_KEY (a strong passphrase) to "
            "enable the Version B replay experiment."
        )
    return key


def store_session(db: Database, host: str, cookies: list[dict],
                  *, member_label: str | None = None) -> dict:
    """Encrypt and upsert a replay session for ``host``."""
    blob = encrypt(json.dumps(cookies), _key())
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
    return json.loads(decrypt(blob, _key()))


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
