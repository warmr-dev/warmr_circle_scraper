"""Credential resolution and authorized session construction.

Deliberately NOT a browser-cookie harvester. Circle's Platform Terms prohibit
third-party scripts that scrape or extract data without prior written consent,
so reusing a logged-in browser session against authenticated Circle HTML is not
an available route. Authorization here comes from operator-issued API tokens.

Secrets are read from the environment at call time and never persisted,
serialized, or logged.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from circle_leads.config.settings import CommunityPermission

logger = logging.getLogger(__name__)

CIRCLE_BASE = os.environ.get("CIRCLE_API_BASE", "https://app.circle.so")
AUTH_TOKEN_PATH = "/api/v1/headless/auth_token"
REFRESH_PATH = "/api/v1/headless/access_token/refresh"

SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}


class MissingCredentialError(RuntimeError):
    """Raised when a required secret is absent from the environment."""


class NotAuthorizedError(RuntimeError):
    """Raised when a community has not been approved for ingestion."""


def redact(mapping: dict) -> dict:
    """Strip credentials before anything reaches a log."""
    return {
        k: ("<redacted>" if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in (mapping or {}).items()
    }


@dataclass
class AdminCredentials:
    """Operator-issued Admin API v2 token for one community."""

    token: str
    # Docs show `Bearer`, the OpenAPI spec's securityScheme says `Token`.
    # Configurable because the two official sources disagree.
    scheme: str = field(
        default_factory=lambda: os.environ.get("CIRCLE_ADMIN_AUTH_SCHEME", "Bearer")
    )

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"{self.scheme} {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "AdminCredentials(token=<redacted>)"


@dataclass
class MemberSession:
    """A community-scoped member JWT minted from a Headless Auth token.

    The operator creates the Headless Auth token; the JWT it mints is scoped to
    one community and one member. It is not a cross-community credential.
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    community_id: int | None = None
    community_member_id: int | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        # Refresh a minute early to avoid racing the boundary.
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(seconds=60)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return (
            f"MemberSession(community_id={self.community_id}, "
            f"access_token=<redacted>, expires_at={self.expires_at})"
        )


def _parse_expiry(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def resolve_admin_credentials(perm: CommunityPermission) -> AdminCredentials:
    """Read the Admin API token for a community from the environment."""
    if not perm.is_approved:
        raise NotAuthorizedError(
            f"Community '{perm.community_id}' has permission_status="
            f"'{perm.permission_status}'. Ingestion requires 'approved'."
        )
    token = perm.secret(perm.admin_token_env)
    if not token:
        raise MissingCredentialError(
            f"No admin token for '{perm.community_id}'. Set the environment "
            f"variable named in admin_token_env ({perm.admin_token_env})."
        )
    return AdminCredentials(token=token)


def mint_member_session(
    perm: CommunityPermission,
    *,
    session: requests.Session | None = None,
    timeout: int = 30,
) -> MemberSession:
    """Exchange the operator's Headless Auth token for a member JWT.

    The Headless Auth token is created by the community admin. Identifying the
    member by ``community_member_id`` (preferred) or ``email`` yields a JWT
    scoped to that community and that member only.
    """
    if not perm.is_approved:
        raise NotAuthorizedError(
            f"Community '{perm.community_id}' is not approved for ingestion."
        )

    auth_token = perm.secret(perm.headless_auth_token_env)
    if not auth_token:
        raise MissingCredentialError(
            f"No headless auth token for '{perm.community_id}'. Set the "
            f"environment variable named in headless_auth_token_env."
        )

    member_id = perm.secret(perm.member_id_env)
    email = os.environ.get(f"CIRCLE_MEMBER_EMAIL__{perm.community_id.replace('-', '_')}")

    # The spec declares oneOf: exactly one identifier may be sent.
    if member_id:
        body = {"community_member_id": int(member_id)}
    elif email:
        body = {"email": email}
    else:
        raise MissingCredentialError(
            f"No member identifier for '{perm.community_id}'. Set either "
            f"{perm.member_id_env} or CIRCLE_MEMBER_EMAIL__<community>."
        )

    http = session or requests.Session()
    resp = http.post(
        CIRCLE_BASE + AUTH_TOKEN_PATH,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    if resp.status_code in (401, 403):
        raise NotAuthorizedError(
            f"Headless auth rejected for '{perm.community_id}' "
            f"(HTTP {resp.status_code}). The operator may have revoked the token."
        )
    resp.raise_for_status()
    data = resp.json()

    logger.info("Minted member session for community '%s'", perm.community_id)
    return MemberSession(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=_parse_expiry(data.get("access_token_expires_at")),
        community_id=data.get("community_id"),
        community_member_id=data.get("community_member_id"),
    )


def refresh_member_session(
    member: MemberSession,
    *,
    session: requests.Session | None = None,
    timeout: int = 30,
) -> MemberSession:
    """Refresh an expiring access token using the refresh token."""
    if not member.refresh_token:
        raise MissingCredentialError("No refresh token available; re-mint instead.")
    http = session or requests.Session()
    resp = http.post(
        CIRCLE_BASE + REFRESH_PATH,
        headers={"Authorization": f"Bearer {member.refresh_token}",
                 "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    member.access_token = data["access_token"]
    member.refresh_token = data.get("refresh_token", member.refresh_token)
    member.expires_at = _parse_expiry(data.get("access_token_expires_at"))
    return member
