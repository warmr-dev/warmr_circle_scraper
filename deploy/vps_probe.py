#!/usr/bin/env python3
"""Test whether a candidate machine can reach Circle without a Cloudflare wall.

Run this on ANY box (a VPS you're evaluating, a Raspberry Pi, a home PC) BEFORE
committing to it. It answers one question: does Circle serve this machine the
community page, or a Cloudflare "unusual traffic" / captcha challenge?

It does NOT log in, store anything, or attempt to defeat a challenge. It loads
the public community URL in a stock headless Chromium and reports what came
back. A challenge here means "run the connector somewhere else"; a clean load
means this machine is a viable home for the connector.

Usage, on the candidate machine:

    pip install playwright
    playwright install --with-deps chromium
    python3 vps_probe.py www.yourspinstate.com community.bigstarlights.com

Exit code 0 if every host loaded clean, 1 if any was challenged/blocked.
"""

from __future__ import annotations

import sys
import urllib.request

# Same broad markers the app uses: a false positive (stop) is safe, a false
# negative (proceed into a wall) is not.
CHALLENGE_MARKERS = (
    "cf-challenge", "cf_chl", "just a moment", "checking your browser",
    "attention required", "captcha", "hcaptcha", "recaptcha", "turnstile",
    "unusual traffic", "verify you are human", "are you a robot",
    "access denied", "request blocked", "ddos protection",
)


def public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            return r.read().decode().strip()
    except Exception:
        return "unknown"


def detect(url: str, html: str) -> str | None:
    hay = f"{url}\n{html[:200000]}".lower()
    for m in CHALLENGE_MARKERS:
        if m in hay:
            return m
    return None


def probe(host: str) -> tuple[str, str]:
    """Return (verdict, detail). verdict in {clean, challenged, error}."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "error", ("Playwright not installed. Run:\n"
                         "  pip install playwright && playwright install --with-deps chromium")

    host = host.replace("https://", "").replace("http://", "").strip("/")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        try:
            page.goto(f"https://{host}", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)  # let a JS challenge redirect land
            html = page.content()
            marker = detect(page.url, html)
            if marker:
                return "challenged", f"Cloudflare/anti-bot wall ({marker}) at {page.url}"
            return "clean", f"Loaded {page.url} with no challenge."
        except Exception as exc:  # noqa: BLE001
            return "error", f"{exc.__class__.__name__}: {exc}"
        finally:
            browser.close()


def main() -> int:
    hosts = sys.argv[1:] or ["www.yourspinstate.com"]
    print(f"This machine's public IP: {public_ip()}")
    print(f"Testing {len(hosts)} host(s) with a stock headless Chromium.\n")

    all_clean = True
    for host in hosts:
        verdict, detail = probe(host)
        icon = {"clean": "✅", "challenged": "🛑", "error": "⚠️"}.get(verdict, "?")
        print(f"{icon} {host:35s} {verdict.upper()}")
        print(f"     {detail}\n")
        if verdict != "clean":
            all_clean = False

    if all_clean:
        print("VERDICT: this machine can reach Circle cleanly. It's a viable home "
              "for the connector — run `circle-connector login <host>` here.")
        return 0
    print("VERDICT: this machine is challenged by Cloudflare. Do NOT run the "
          "connector here — use a residential machine (home PC / Pi) instead. "
          "Nothing here tries to bypass the challenge.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
