"""Server-hosted remote browser: challenge policy and the PoC report.

No real browser and no network here. The point of these tests is the policy:
a verification challenge must STOP the run and be reported, never retried or
worked around. (The dashboard routes that used to expose this -- the Remote
Browser tab -- were removed outright; see the note further down.)
"""

from __future__ import annotations

import pytest

from circle_leads.remote_browser.probe import probe_authenticated, probe_environment
from circle_leads.remote_browser.session import (
    ChallengeDetected, SessionState, SessionStatus, detect_challenge,
)


# --- challenge detection ---------------------------------------------------

@pytest.mark.parametrize("url,html,expected", [
    ("https://x.circle.so", "<h1>Just a moment...</h1>", "just a moment"),
    ("https://x.circle.so", "<div class='cf-challenge'>", "cf-challenge"),
    ("https://x.circle.so", "please complete the CAPTCHA", "captcha"),
    ("https://x.circle.so", "<div>Checking your browser</div>", "checking your browser"),
    ("https://x.circle.so", "Verify you are human", "verify you are human"),
    ("https://x.circle.so/cf_chl_jschl", "<html></html>", "cf_chl"),
    ("https://x.circle.so", "Access Denied", "access denied"),
])
def test_detect_challenge_flags_verification_walls(url, html, expected):
    assert detect_challenge(url, html) == expected


def test_detect_challenge_ignores_a_normal_page():
    html = "<html><body><h1>Welcome to the community</h1><a>Sign in</a></body></html>"
    assert detect_challenge("https://x.circle.so", html) is None


def test_detect_challenge_is_case_insensitive_and_null_safe():
    # "captcha" matches first, being a substring of "hcaptcha" -- either way
    # the wall is detected, which is the behaviour that matters.
    assert detect_challenge("", "hCaptcha widget") is not None
    assert detect_challenge(None, None) is None


# --- the PoC report --------------------------------------------------------

class _FakeBrowser:
    """Stands in for RemoteBrowser; records what the probe asked it to do."""

    def __init__(self, *, status, spaces=None, screenshot=b"PNGDATA",
                 start_error=None, challenge_on_fetch=False):
        self.profile_dir = "/tmp/profile"
        self._status = status
        self._spaces = spaces
        self._screenshot = screenshot
        self._start_error = start_error
        self._challenge_on_fetch = challenge_on_fetch
        self.navigations = 0

    def start(self, host=None):
        if self._start_error:
            raise self._start_error
        return self._status

    def screenshot(self):
        return self._screenshot

    def navigate(self, host):
        self.navigations += 1
        return self._status

    def refresh_status(self):
        return self._status

    def fetch_json(self, path):
        if self._challenge_on_fetch:
            raise ChallengeDetected("Circle responded with HTTP 429. Stopping.")
        return {"records": self._spaces or []}


def test_probe_reports_blocked_and_stops_when_circle_challenges():
    st = SessionStatus(state=SessionState.CHALLENGED.value,
                       detail="Circle presented a verification challenge (captcha).",
                       challenge_url="https://x.circle.so/cdn-cgi/challenge")
    browser = _FakeBrowser(status=st)
    report = probe_environment(browser, "x.circle.so")

    assert report.verdict == "BLOCKED_BY_CHALLENGE"
    assert "no bypass is attempted" in report.summary.lower()
    # It must not keep hammering the challenge page.
    assert browser.navigations == 1


def test_probe_reports_no_browser_when_playwright_missing():
    from circle_leads.remote_browser.session import RemoteBrowserUnavailable

    browser = _FakeBrowser(status=SessionStatus(),
                           start_error=RemoteBrowserUnavailable("not installed"))
    report = probe_environment(browser, "x.circle.so")
    assert report.verdict == "NO_BROWSER"
    assert any(not s.ok for s in report.steps)


def test_probe_environment_passes_when_circle_serves_the_server_browser():
    st = SessionStatus(state=SessionState.AWAITING_LOGIN.value,
                       current_url="https://x.circle.so/", host="x.circle.so")
    report = probe_environment(_FakeBrowser(status=st), "x.circle.so")
    assert report.verdict == "NEEDS_LOGIN"
    assert report.steps[0].ok is True          # Chromium started
    assert report.steps[1].ok is True          # interactive view rendered


def test_authenticated_probe_is_viable_when_it_reads_private_spaces():
    st = SessionStatus(state=SessionState.AUTHENTICATED.value,
                       member_label="Yer", host="x.circle.so")
    browser = _FakeBrowser(status=st, spaces=[{"id": 1}, {"id": 2}, {"id": 3}])
    report = probe_authenticated(browser, "x.circle.so")

    assert report.verdict == "VIABLE"
    assert "3 space(s)" in report.summary
    assert all(s.ok for s in report.steps)


def test_authenticated_probe_stops_if_challenged_during_reads():
    st = SessionStatus(state=SessionState.AUTHENTICATED.value,
                       member_label="Yer", host="x.circle.so")
    browser = _FakeBrowser(status=st, challenge_on_fetch=True)
    report = probe_authenticated(browser, "x.circle.so")
    assert report.verdict == "BLOCKED_BY_CHALLENGE"


def test_authenticated_probe_requires_a_login_first():
    st = SessionStatus(state=SessionState.AWAITING_LOGIN.value, host="x.circle.so")
    report = probe_authenticated(_FakeBrowser(status=st), "x.circle.so")
    assert report.verdict == "NEEDS_LOGIN"


def test_report_carries_no_credentials():
    st = SessionStatus(state=SessionState.AUTHENTICATED.value,
                       member_label="Yer", host="x.circle.so")
    report = probe_authenticated(_FakeBrowser(status=st, spaces=[{"id": 1}]), "x.circle.so")
    blob = repr(report.as_dict()).lower()
    for banned in ("cookie", "password", "session_token", "set-cookie", "bearer"):
        assert banned not in blob


# The dashboard routes that used to be tested here (/api/remote-browser/*,
# built by circle_leads/web/remote_browser_api.py) were removed outright: the
# Remote Browser tab was explicitly labeled "proven non-working
# (Cloudflare-blocked)... kept for the PoC record" and shipped hidden. The
# underlying circle_leads/remote_browser/ package (challenge detection, the
# PoC report, the browser thread) stays -- it's not dashboard-specific and
# untouched by that cleanup -- so its unit tests below still apply.


# --- the browser thread ----------------------------------------------------

def test_browser_thread_runs_calls_and_relays_exceptions():
    """Playwright's sync API is pinned to its creating thread, so all browser
    work must land on one dedicated thread. Regression: calling from a web
    server's rotating threadpool used to raise
    'greenlet.error: cannot switch to a different thread'."""
    from circle_leads.remote_browser.session import _BrowserThread
    import threading

    worker = _BrowserThread()
    try:
        seen = []
        # Every call must execute on the SAME thread, whichever thread submits.
        def record():
            seen.append(threading.current_thread().name)
            return "value"

        assert worker.call(record) == "value"

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(worker.call(record)))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == ["value"] * 4
        assert len(set(seen)) == 1, "browser work leaked onto multiple threads"
        assert seen[0] == "remote-browser"

        # Exceptions surface to the caller rather than killing the thread.
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            worker.call(boom)
        assert worker.call(record) == "value"  # still alive afterwards
    finally:
        worker.shutdown()
