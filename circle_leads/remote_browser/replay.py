"""EXPERIMENT (Version B): replay a captured Circle session on the server.

Built at the user's explicit, on-record request, overriding the project's
original "no cookie export/replay from Railway" rule. It is kept isolated and
clearly labeled so it is easy to reason about and to remove.

What it does:
  1. Parse a pasted cookie export (Chrome/EditThisCookie JSON).
  2. Load those cookies into a fresh headless browser context.
  3. Ask Circle a member-only endpoint whether the session is valid FROM HERE.
  4. Report the honest result: ok / challenged / expired / error.

The expectation, stated plainly, is that this fails from a datacenter:
Circle binds ``cf_clearance`` to the original IP+device, so a replay from
Railway's IP is re-challenged, and ``__cf_bm`` expires within minutes. This
module measures that rather than pretending otherwise, and it never tries to
solve or evade a challenge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from circle_leads.remote_browser.session import (
    RemoteBrowserUnavailable, _require_playwright, detect_challenge,
)

# Cookies whose presence indicates a real logged-in Circle member session.
_SESSION_COOKIE_NAMES = {"_circle_session", "remember_user_token", "user_session_identifier"}

# The same member-only endpoints the connector uses.
_LOGIN_CHECK_PATHS = (
    "/internal_api/current_community_member",
    "/internal_api/community_members/me",
    "/internal_api/me",
)


@dataclass
class ReplayResult:
    result: str          # ok | challenged | expired | error
    detail: str = ""
    member_label: str | None = None
    spaces: int | None = None

    def as_dict(self) -> dict:
        return {"result": self.result, "detail": self.detail,
                "member_label": self.member_label, "spaces": self.spaces}


def parse_cookies(raw: str | list) -> list[dict]:
    """Normalize a pasted cookie export into Playwright's cookie shape.

    Accepts a JSON string or an already-parsed list. Raises ValueError on
    anything that isn't a cookie array.
    """
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of cookies.")
    out: list[dict] = []
    same_site_map = {
        "lax": "Lax", "strict": "Strict", "no_restriction": "None",
        "none": "None", "unspecified": None,
    }
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c:
            continue
        domain = str(c.get("domain") or "").strip()
        if not domain:
            continue
        cookie = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": domain,
            "path": str(c.get("path") or "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        exp = c.get("expirationDate")
        if exp and not c.get("session"):
            cookie["expires"] = float(exp)
        ss = same_site_map.get(str(c.get("sameSite") or "").lower(), None)
        if ss:
            cookie["sameSite"] = ss
        out.append(cookie)
    if not out:
        raise ValueError("No usable cookies found in the export.")
    return out


def has_session_cookie(cookies: list[dict]) -> bool:
    names = {c.get("name") for c in cookies}
    return bool(names & _SESSION_COOKIE_NAMES)


def _primary_host(cookies: list[dict]) -> str | None:
    """Best guess at the community host from the cookie domains."""
    for c in cookies:
        if c.get("name") in _SESSION_COOKIE_NAMES:
            return str(c.get("domain") or "").lstrip(".")
    for c in cookies:
        d = str(c.get("domain") or "").lstrip(".")
        if d and "circle" not in d and d.count(".") >= 1:
            return d
    return None


def replay_session(host: str, cookies: list[dict], *, headless: bool = True) -> ReplayResult:
    """Load ``cookies`` into a fresh browser and check the session from HERE.

    Returns an honest verdict. Never attempts to defeat a challenge.
    """
    host = host.replace("https://", "").replace("http://", "").strip("/")
    base = f"https://{host}"
    sync_playwright = _require_playwright()  # raises RemoteBrowserUnavailable

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless,
                                     args=["--disable-dev-shm-usage", "--no-sandbox"])
        ctx = browser.new_context()
        try:
            try:
                ctx.add_cookies(cookies)
            except Exception as exc:  # noqa: BLE001 - malformed cookie shape
                return ReplayResult("error", f"Could not load cookies: {exc}")

            page = ctx.new_page()
            page.goto(base, wait_until="domcontentloaded", timeout=45_000)

            marker = detect_challenge(page.url, _safe_content(page))
            if marker:
                return ReplayResult(
                    "challenged",
                    f"Circle challenged the replayed session ({marker}) at {page.url}. "
                    "As expected, a session captured elsewhere is re-verified from "
                    "this server's IP.",
                )

            # Session valid from here?
            member = None
            for path in _LOGIN_CHECK_PATHS:
                try:
                    resp = page.request.get(base + path, headers={"Accept": "application/json"})
                except Exception:
                    continue
                if resp.status in (401, 403):
                    return ReplayResult(
                        "expired",
                        "Circle rejected the replayed session (401/403). The cookies "
                        "are not valid from this server.",
                    )
                if resp.status == 200:
                    try:
                        body = resp.json()
                        body = body.get("community_member") or body.get("user") or body
                        member = body.get("name") or body.get("full_name") or "signed in"
                    except Exception:
                        member = "signed in"
                    break

            if member is None:
                return ReplayResult("expired", "No valid member session from this server.")

            # It worked from here — try to read the spaces list too.
            spaces = None
            try:
                sp = page.request.get(base + "/internal_api/spaces",
                                      headers={"Accept": "application/json"})
                if sp.status == 200:
                    payload = sp.json()
                    recs = (payload.get("records") if isinstance(payload, dict) else payload) or []
                    spaces = len(recs)
            except Exception:
                pass

            return ReplayResult("ok",
                                f"Replayed session is valid from this server as {member}.",
                                member_label=member, spaces=spaces)
        finally:
            ctx.close()
            browser.close()


def _safe_content(page) -> str:
    try:
        return page.content()
    except Exception:
        return ""
