"""Password gate for the dashboard.

The password comes from the environment, never from source. The dashboard
reads a database of other people's posts, so it refuses to start
unauthenticated rather than defaulting to open.
"""

from __future__ import annotations

import hmac
import os
import secrets
from datetime import timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

PASSWORD_ENV = "DASHBOARD_PASSWORD"
SECRET_ENV = "DASHBOARD_SECRET_KEY"
COOKIE_NAME = "circle_leads_session"
SESSION_MAX_AGE = int(timedelta(days=7).total_seconds())


class AuthNotConfigured(RuntimeError):
    """Raised when no dashboard password is set."""


def get_password() -> str:
    password = os.environ.get(PASSWORD_ENV, "").strip()
    if not password:
        raise AuthNotConfigured(
            f"{PASSWORD_ENV} is not set. Add it to .env (which is gitignored):\n"
            f"  {PASSWORD_ENV}=<a password you choose>\n"
            "The dashboard will not start without one."
        )
    if len(password) < 8:
        raise AuthNotConfigured(
            f"{PASSWORD_ENV} must be at least 8 characters."
        )
    return password


def get_secret_key() -> str:
    """Signing key for the session cookie.

    A generated key is fine locally -- it only means sessions end when the
    server restarts. For a deployment, set it so sessions survive a redeploy.
    """
    return os.environ.get(SECRET_ENV, "").strip() or secrets.token_urlsafe(32)


def verify_password(candidate: str) -> bool:
    """Constant-time comparison, so timing does not leak the password."""
    try:
        expected = get_password()
    except AuthNotConfigured:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


class SessionManager:
    def __init__(self, secret_key: str | None = None):
        self._serializer = URLSafeTimedSerializer(
            secret_key or get_secret_key(), salt="circle-leads-dashboard"
        )

    def issue(self) -> str:
        return self._serializer.dumps({"authenticated": True})

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            data = self._serializer.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return False
        return bool(data.get("authenticated"))
