"""The proof of concept: can a server-hosted browser hold a Circle session?

Runs the PoC as specified and reports a verdict BEFORE anything is wired into
the production scraper:

  1. Start Chromium on the server.
  2. Expose a secure interactive view.        (the dashboard's Remote Browser tab)
  3. Navigate to Circle.
  4. Authenticate manually, in that browser.
  5. Verify the same browser is still authenticated afterwards.
  6. Test access to a private community the account is enrolled in.
  7. Measure whether Circle challenges a datacenter browser.
  8. Report the result.

Steps 3-4 are interactive, so the probe is split: ``probe_environment`` covers
what can be checked with no login (1, 2, 7), and ``probe_authenticated`` covers
what needs one (5, 6, 7). Neither ever tries to defeat a challenge -- a
challenge is recorded as the finding.
"""

from __future__ import annotations

import platform
import time
from dataclasses import dataclass, field

from circle_leads.remote_browser.session import (
    ChallengeDetected, RemoteBrowser, RemoteBrowserUnavailable, SessionState,
    detect_challenge,
)


@dataclass
class ProbeStep:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ProbeReport:
    """The PoC verdict. ``verdict`` is one of:

    VIABLE               -- a server browser holds the session and reads a private community
    BLOCKED_BY_CHALLENGE -- Circle challenges the datacenter browser; stop here
    NO_BROWSER           -- Chromium/Playwright missing from the image
    NEEDS_LOGIN          -- environment is fine; a human still has to sign in
    FAILED               -- something else broke
    """

    verdict: str = "UNKNOWN"
    summary: str = ""
    steps: list[ProbeStep] = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(ProbeStep(name=name, ok=ok, detail=detail))

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "steps": [s.as_dict() for s in self.steps],
            "environment": self.environment,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _environment() -> dict:
    """Non-sensitive facts about where this is running."""
    import os

    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        # Railway sets these; their presence is what tells us we're in a
        # datacenter rather than on a laptop.
        "railway_environment": os.environ.get("RAILWAY_ENVIRONMENT_NAME"),
        "railway_region": os.environ.get("RAILWAY_REPLICA_REGION"),
        "is_datacenter": bool(os.environ.get("RAILWAY_ENVIRONMENT_NAME")),
    }


def probe_environment(browser: RemoteBrowser, host: str) -> ProbeReport:
    """Steps 1, 2, 3 and 7 -- everything checkable without a login."""
    report = ProbeReport(environment=_environment())

    # Step 1: Chromium starts here at all.
    try:
        browser.start()
        report.add("1. Chromium starts on the server", True,
                   f"Persistent profile at {browser.profile_dir}")
    except RemoteBrowserUnavailable as exc:
        report.add("1. Chromium starts on the server", False, str(exc))
        report.verdict = "NO_BROWSER"
        report.summary = ("Chromium/Playwright is not in this image. Add the "
                          "browser extra and `playwright install --with-deps "
                          "chromium` to the Dockerfile.")
        report.finished_at = time.time()
        return report
    except Exception as exc:  # noqa: BLE001 - report, don't crash the request
        report.add("1. Chromium starts on the server", False,
                   f"{exc.__class__.__name__}: {exc}")
        report.verdict = "FAILED"
        report.summary = "Chromium could not be launched here."
        report.finished_at = time.time()
        return report

    # Step 2: the interactive view is a screenshot + input relay.
    try:
        png = browser.screenshot()
        report.add("2. Interactive view renders", True, f"{len(png)} byte PNG")
    except Exception as exc:  # noqa: BLE001
        report.add("2. Interactive view renders", False, f"{exc.__class__.__name__}: {exc}")

    # Step 3 + 7: reach Circle and see how it treats a datacenter browser.
    try:
        status = browser.navigate(host)
    except Exception as exc:  # noqa: BLE001
        report.add(f"3. Reach https://{host}", False, f"{exc.__class__.__name__}: {exc}")
        report.verdict = "FAILED"
        report.summary = f"Could not load {host} from this environment."
        report.finished_at = time.time()
        return report

    if status.state == SessionState.CHALLENGED.value:
        report.add(f"3. Reach https://{host}", False, status.detail)
        report.add("7. Circle challenges a datacenter browser", True, status.detail)
        report.verdict = "BLOCKED_BY_CHALLENGE"
        report.summary = (
            f"Circle presented a verification challenge to the server browser at "
            f"{status.challenge_url}. Stopping as instructed -- no bypass is "
            "attempted. Use the local agent instead."
        )
        report.finished_at = time.time()
        return report

    report.add(f"3. Reach https://{host}", True, f"Loaded {status.current_url}")
    report.add("7. Circle challenges a datacenter browser", True,
               "No challenge on the landing page.")

    if status.state == SessionState.AUTHENTICATED.value:
        report.verdict = "NEEDS_LOGIN"  # refined by probe_authenticated
        report.summary = (f"Already signed in as {status.member_label}. "
                          "Run the authenticated probe to finish steps 5-6.")
    else:
        report.verdict = "NEEDS_LOGIN"
        report.summary = ("The server browser reaches Circle unchallenged. Log in "
                          "through the live view, then run the authenticated probe.")
    report.finished_at = time.time()
    return report


def probe_authenticated(browser: RemoteBrowser, host: str) -> ProbeReport:
    """Steps 5, 6 and 7 -- run after you have signed in via the live view."""
    report = ProbeReport(environment=_environment())

    # Step 5: is the SAME browser still authenticated?
    status = browser.refresh_status()
    if status.state == SessionState.CHALLENGED.value:
        report.add("5. Session persists in the server browser", False, status.detail)
        report.add("7. Circle challenges a datacenter browser", True, status.detail)
        report.verdict = "BLOCKED_BY_CHALLENGE"
        report.summary = status.detail
        report.finished_at = time.time()
        return report

    if status.state != SessionState.AUTHENTICATED.value:
        report.add("5. Session persists in the server browser", False,
                   f"State is {status.state}: {status.detail}")
        report.verdict = "NEEDS_LOGIN"
        report.summary = "No signed-in session yet. Log in via the live view first."
        report.finished_at = time.time()
        return report

    report.add("5. Session persists in the server browser", True,
               f"Still signed in as {status.member_label}.")

    # Step 6: can it actually read a private community's spaces?
    try:
        payload = browser.fetch_json("/internal_api/spaces")
    except ChallengeDetected as exc:
        report.add("6. Read a private community", False, str(exc))
        report.add("7. Circle challenges a datacenter browser", True, str(exc))
        report.verdict = "BLOCKED_BY_CHALLENGE"
        report.summary = str(exc)
        report.finished_at = time.time()
        return report

    records = (payload.get("records") if isinstance(payload, dict) else payload) or []
    if not records:
        report.add("6. Read a private community", False,
                   "Signed in, but no spaces are visible to this account.")
        report.verdict = "FAILED"
        report.summary = (f"Authenticated to {host} but the account sees no spaces. "
                          "Check the account is actually enrolled there.")
        report.finished_at = time.time()
        return report

    report.add("6. Read a private community", True,
               f"{len(records)} space(s) visible to this account.")
    report.add("7. Circle challenges a datacenter browser", True,
               "No challenge during authenticated reads.")

    report.verdict = "VIABLE"
    report.summary = (
        f"The server-hosted browser holds the Circle session and reads {len(records)} "
        f"space(s) in {host}. The session originated in this browser -- nothing was "
        "exported or replayed."
    )
    report.finished_at = time.time()
    return report
