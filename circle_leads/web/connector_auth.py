"""Pairing and token auth for the local Circle Connector.

The connector runs on the user's own computer and holds the authenticated
Circle browser session there. Railway must be able to trust uploads from it
WITHOUT ever seeing Circle credentials. The scheme:

  1. Dashboard mints a short-lived one-time PAIRING CODE (a Connector row).
  2. The user runs the local connector and enters that code.
  3. The connector POSTs the code once; the server issues a long-lived CONNECTOR
     TOKEN, returns it exactly once, and stores only its SHA-256 hash.
  4. Every later heartbeat/upload carries `Authorization: Bearer <token>`; the
     server matches its hash to a paired Connector.

No Circle secret ever touches this path -- it only authenticates the connector
process to the backend.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from circle_leads.storage.database import Database
from circle_leads.storage.models import Connector

PAIRING_TTL_MINUTES = 15


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_pairing(db: Database, *, name: str | None = None) -> dict:
    """Mint a one-time pairing code. Returns the code (shown once in the UI)."""
    code = secrets.token_hex(4).upper()  # 8 hex chars, easy to type
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        minutes=PAIRING_TTL_MINUTES
    )
    with db.session() as s:
        c = Connector(name=name, pairing_code=code, pairing_expires_at=expires,
                      paired=False)
        s.add(c)
        s.flush()
        cid = c.id
    return {"connector_id": cid, "pairing_code": code,
            "expires_in_minutes": PAIRING_TTL_MINUTES}


def claim_pairing(db: Database, code: str, *, agent_info: str | None = None) -> dict | None:
    """Exchange a valid pairing code for a connector token (returned ONCE).

    Returns None if the code is unknown, already used, or expired.
    """
    code = (code or "").strip().upper()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db.session() as s:
        c = s.scalar(
            select(Connector).where(
                Connector.pairing_code == code, Connector.paired.is_(False)
            )
        )
        if c is None:
            return None
        if c.pairing_expires_at is not None and c.pairing_expires_at < now:
            return None
        token = secrets.token_urlsafe(32)
        c.token_hash = _hash(token)
        c.paired = True
        c.pairing_code = None          # one-time: burn the code
        c.pairing_expires_at = None
        c.agent_info = agent_info
        c.last_seen_at = now
        cid = c.id
    # Token is returned to the connector here and never stored in plaintext.
    return {"connector_id": cid, "token": token}


def verify_connector_token(db: Database, token: str | None) -> Connector | None:
    """Return the paired Connector for a bearer token, or None."""
    if not token:
        return None
    th = _hash(token.strip())
    with db.session() as s:
        c = s.scalar(
            select(Connector).where(
                Connector.token_hash == th, Connector.paired.is_(True)
            )
        )
        if c is not None:
            c.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
            # expunge so the caller can read attrs after the session closes
            s.expunge(c)
        return c
