"""Read Angel Squad after it left Circle.

The member app at https://app.angelsquad.co signs in with an emailed code and
serves its feed from https://api.angelsquad.co. That API uses the browser's
own session cookie. A Circle cookie does not open it.
"""

from __future__ import annotations

from typing import Any

import requests
from sqlalchemy import select

from circle_leads.storage.database import Database
from circle_leads.storage.models import Community, WatchMode, WatchState

APP_HOST = "app.angelsquad.co"
LEGACY_HOST = "angel-squad.circle.so"
API_ORIGIN = "https://api.angelsquad.co"
_FEED = "/api/community/feed"
_ME = "/api/me"


def is_angelsquad_host(host: str | None) -> bool:
    name = (host or "").lower().strip().strip(".")
    return name in {APP_HOST, LEGACY_HOST, "api.angelsquad.co"} or name.endswith(".angelsquad.co")


def canonical_host(host: str) -> str:
    """Sessions and reads for this community live on the app host."""
    return APP_HOST if is_angelsquad_host(host) else host


def accepted_cookies(host: str, items: list[dict] | None) -> dict[str, str]:
    """Cookies worth storing for ``host``.

    Circle communities keep only the two session cookies. Angel Squad's cookie
    name is set by its own server, so every cookie the browser holds for that
    site is kept.
    """
    from circle_leads.scraper.member_api_reader import SESSION_COOKIE_NAMES

    circle_only = not is_angelsquad_host(host)
    out: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if not name or not value:
            continue
        if circle_only and name not in SESSION_COOKIE_NAMES:
            continue
        out[name] = value
    return out


def retarget_legacy(db: Database) -> None:
    """Point the old Circle row at the app, once, when a real session arrives."""
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.host == LEGACY_HOST))
        if row is None:
            return
        row.host = APP_HOST
        row.url = f"https://{APP_HOST}"
        row.platform = "other"
        row.watching = True
        row.notes = "Left Circle. The member feed is the Angel Squad app."
        watch = s.scalar(select(WatchState).where(WatchState.community_id == row.id))
        if watch is not None:
            watch.host = APP_HOST
            watch.mode = WatchMode.COOKIE.value


def verify(cookies: dict[str, str], *, timeout: float = 15.0) -> dict[str, Any]:
    """One ``GET /api/me``. Same verdicts as a Circle session check."""
    try:
        response = requests.get(
            API_ORIGIN + _ME,
            cookies=cookies,
            headers={"Accept": "application/json"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return {"verdict": "unverified", "spaces": 0,
                "detail": f"Could not reach Angel Squad: {type(exc).__name__}."}
    if response.status_code == 401:
        return {"verdict": "rejected", "spaces": 0,
                "detail": "Angel Squad rejected the session. Log in at "
                          "https://app.angelsquad.co and send that site's cookies."}
    if response.status_code == 403:
        return {"verdict": "forbidden", "spaces": 0,
                "detail": "Angel Squad answered 403 to the session check."}
    if "application/json" not in response.headers.get("content-type", ""):
        return {"verdict": "unverified", "spaces": 0,
                "detail": "Angel Squad answered without JSON; the worker will check it."}
    if response.status_code >= 400:
        return {"verdict": "unverified", "spaces": 0,
                "detail": f"Angel Squad answered HTTP {response.status_code}."}
    return {"verdict": "valid", "spaces": 1,
            "detail": "Angel Squad accepted the session."}


def fetch_records(cookies: dict[str, str], *, timeout: float = 25.0,
                  max_pages: int = 5) -> tuple[str, str, list[dict]]:
    """The member feed as Circle-shaped post records.

    Returns ``(status, detail, records)``. ``status`` is ``valid``,
    ``rejected``, ``forbidden`` or ``unverified``.
    """
    records: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params = {"sort": "newest"}
        if cursor:
            params["cursor"] = cursor
        try:
            response = requests.get(
                API_ORIGIN + _FEED,
                params=params,
                cookies=cookies,
                headers={"Accept": "application/json"},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            return "unverified", f"Could not reach Angel Squad: {type(exc).__name__}.", []
        if response.status_code == 401:
            return ("rejected",
                    "Angel Squad rejected the session. Log in at "
                    "https://app.angelsquad.co and send that site's cookies.", [])
        if response.status_code == 403:
            return "forbidden", "Angel Squad answered 403.", []
        if "application/json" not in response.headers.get("content-type", ""):
            return "unverified", "Angel Squad answered without JSON.", []
        if response.status_code >= 400:
            return "unverified", f"Angel Squad answered HTTP {response.status_code}.", []
        try:
            payload = response.json() or {}
        except ValueError:
            return "unverified", "Angel Squad answered without JSON.", []
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return "unverified", "Angel Squad feed had no items list.", records
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                records.append(_as_circle_record(item))
        cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
        if not cursor:
            break
    return "valid", f"Angel Squad feed listed {len(records)} post(s).", records


def _as_circle_record(item: dict) -> dict:
    post_id = str(item["id"])
    return {
        "id": post_id,
        "name": item.get("title") or "",
        "body_plain_text": item.get("body") or item.get("excerpt") or "",
        "created_at": item.get("publishedAt") or item.get("lastActivityAt"),
        "slug": post_id,
        "url": f"https://{APP_HOST}/community/posts/{post_id}",
        "space_slug": "community",
        "user": {"id": item.get("authorId"), "name": item.get("authorName")},
    }
