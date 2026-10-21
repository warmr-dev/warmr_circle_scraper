"""Remote browser session: challenge policy and browser thread.

No real browser and no network here. The point of these tests is the policy:
a verification challenge must STOP the run and be reported, never retried or
worked around. (The dashboard routes that used to expose this -- the Remote
Browser tab -- were removed outright; see the note further down.)
"""

from __future__ import annotations

import pytest

from circle_leads.remote_browser.session import detect_challenge


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
