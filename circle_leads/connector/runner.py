"""Orchestrates one connector cycle for a private Circle community.

For a host the user provided:
  1. Check the local browser session (never touches Circle credentials).
  2. If not logged in -> report AUTHENTICATION_REQUIRED (and, interactively,
     open a window for the user to log in themselves).
  3. If logged in -> enumerate accessible spaces, read their posts locally,
     normalize, and upload ONLY the normalized records to Railway.
  4. Report the connection state at each step so the dashboard reflects reality.

The three facts the spec insists stay separate are tracked separately:
  session valid  ->  community accessible  ->  spaces readable  ->  content sent
"""

from __future__ import annotations

import logging

from circle_leads.connector.client import BackendClient
from circle_leads.scraper.browser_reader import (
    BrowserFeedReader, BrowserNotAvailable, NotLoggedIn, fetch_space_posts,
)
from circle_leads.storage.models import ConnectionState

logger = logging.getLogger(__name__)


def authenticate(host: str, *, timeout_seconds: int = 300) -> None:
    """Open a visible browser so the user logs into `host` themselves."""
    reader = BrowserFeedReader(host, headless=False)
    reader.login(timeout_seconds=timeout_seconds)


def authenticate_with_credentials(
    host: str, *, headless: bool = True, timeout_seconds: int = 120
) -> None:
    """Sign in to `host` using credentials from the environment.

    The credentials never leave this machine and are never logged. If Circle
    presents a Cloudflare or 2FA challenge, this raises so the caller can fall
    back to an interactive login rather than trying to defeat it.
    """
    from circle_leads.connector.credentials import load_for_host

    creds = load_for_host(host)  # raises CredentialsNotFound with a clear message
    reader = BrowserFeedReader(host, headless=headless)
    reader.login_with_credentials(
        creds.email, creds.password,
        headless=headless, timeout_seconds=timeout_seconds,
    )


def sync_community_http(
    host: str,
    backend: BackendClient,
    *,
    excluded_content: list[str] | None = None,
    max_pages: int = 5,
) -> dict:
    """Sync a community over plain HTTP using the member session cookie.

    The browser logged in once and saved a session to the local profile; this
    exports that cookie and does every read with a plain HTTP client (no
    browser, no Cloudflare). Falls back to reporting AUTHENTICATION_REQUIRED if
    the session isn't valid, exactly like the browser path.
    """
    from circle_leads.scraper.browser_reader import BrowserFeedReader, NotLoggedIn
    from circle_leads.scraper.member_api_reader import (
        MemberApiReader, SessionInvalid, fetch_space_posts as http_fetch,
    )

    host = host.replace("https://", "").strip("/")
    # One browser touch to lift the session cookie; everything else is HTTP.
    try:
        cookies = BrowserFeedReader(host, headless=True).export_session_cookies()
    except NotLoggedIn:
        backend.report_connection(
            host=host, state=ConnectionState.AUTHENTICATION_REQUIRED.value,
            state_detail="No valid local session -- please authenticate.",
        )
        return {"host": host, "state": "authentication_required"}

    reader = MemberApiReader(host, cookies=cookies)
    try:
        if not reader.check_session():
            backend.report_connection(
                host=host, state=ConnectionState.SESSION_EXPIRED.value,
                state_detail="Session cookie expired.",
            )
            return {"host": host, "state": "session_expired"}
        spaces = reader.list_spaces()
    except SessionInvalid:
        backend.report_connection(host=host, state=ConnectionState.SESSION_EXPIRED.value)
        return {"host": host, "state": "session_expired"}

    if not spaces:
        backend.report_connection(host=host, state=ConnectionState.ACCESS_DENIED.value,
                                  state_detail="No spaces visible to this account.")
        return {"host": host, "state": "access_denied", "spaces": 0}

    total_posts = 0
    readable = 0
    for sp in spaces:
        try:
            records = http_fetch(reader, sp["id"], excluded_content=excluded_content,
                                 max_pages=max_pages)
        except SessionInvalid:
            backend.report_connection(host=host, state=ConnectionState.SESSION_EXPIRED.value)
            return {"host": host, "state": "session_expired", "posts": total_posts}
        if not records:
            continue
        readable += 1
        total_posts += len(records)
        backend.ingest(host, records)

    backend.report_connection(
        host=host, state=ConnectionState.CONNECTED.value,
        spaces_total=len(spaces), spaces_readable=readable,
    )
    return {"host": host, "state": "connected", "spaces_total": len(spaces),
            "spaces_readable": readable, "posts": total_posts, "transport": "http"}


def sync_community(
    host: str,
    backend: BackendClient,
    *,
    excluded_content: list[str] | None = None,
    max_pages: int = 5,
) -> dict:
    """Run one read+upload cycle for a connected community. Reports state to
    the backend throughout. Returns a summary dict."""
    host = host.replace("https://", "").strip("/")
    try:
        reader = BrowserFeedReader(host, headless=True)
    except BrowserNotAvailable as exc:
        backend.report_connection(host=host, state=ConnectionState.ERROR.value,
                                  state_detail=str(exc))
        return {"host": host, "state": "error", "error": str(exc)}

    # 1. Session valid?  (logged-in-ness is distinct from access)
    if not reader.status():
        backend.report_connection(
            host=host, state=ConnectionState.AUTHENTICATION_REQUIRED.value,
            state_detail="No valid local session — please authenticate.",
        )
        return {"host": host, "state": "authentication_required"}

    # 2 + 3. Enumerate spaces the account can access.
    try:
        spaces = reader.list_spaces()
    except NotLoggedIn:
        backend.report_connection(host=host, state=ConnectionState.SESSION_EXPIRED.value,
                                  state_detail="Session expired mid-sync.")
        return {"host": host, "state": "session_expired"}

    if not spaces:
        # Logged in but no readable spaces -> access is denied/empty.
        backend.report_connection(host=host, state=ConnectionState.ACCESS_DENIED.value,
                                  state_detail="No spaces visible to this account.")
        return {"host": host, "state": "access_denied", "spaces": 0}

    # 4. Read + normalize + upload each space's posts.
    total_posts = 0
    readable_spaces = 0
    for sp in spaces:
        try:
            records = fetch_space_posts(
                reader, sp["id"], excluded_content=excluded_content, max_pages=max_pages
            )
        except NotLoggedIn:
            backend.report_connection(host=host, state=ConnectionState.SESSION_EXPIRED.value)
            return {"host": host, "state": "session_expired", "posts": total_posts}
        if not records:
            continue
        readable_spaces += 1
        total_posts += len(records)
        backend.ingest(host, records)  # normalized records only

    backend.report_connection(
        host=host, state=ConnectionState.CONNECTED.value,
        spaces_total=len(spaces), spaces_readable=readable_spaces,
    )
    return {"host": host, "state": "connected",
            "spaces_total": len(spaces), "spaces_readable": readable_spaces,
            "posts": total_posts}
