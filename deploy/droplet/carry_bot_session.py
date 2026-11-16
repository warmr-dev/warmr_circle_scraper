"""Carry the join bot's Circle sign-in from a browser that has it to one that does not.

Why: since the move to circle-bot-prod (2026-11-06) Circle's central login
(login.circle.so) shows the droplet a Cloudflare Turnstile checkbox after the
email, so the droplet's browser cannot sign the bot in by itself. Opening a
community through the central session it already holds needs no email and met
no check (2026-11-09). So the bot signs in on the Mac, and its cookies are
carried here. Nothing about the checkbox is touched: when the central session
ends, a full sign-in from the droplet stops as `challenge_stop` and this is
run again.

    # on the Mac, with the bot's Chrome open on CDP 127.0.0.1:9222:
    python deploy/droplet/carry_bot_session.py export \
      | ssh root@<droplet> /opt/warmr/current/venv/bin/python \
          /opt/warmr/current/deploy/droplet/carry_bot_session.py import

The values travel only through that pipe: `export` refuses to write them to a
terminal, `import` prints counts. Carried: Circle's cookies only -- a host that
holds a `_circle_session` (login.circle.so and each community), its parent and
child domains, and *.circle.so. The bot's profile also holds Google, payment
and other cookies; those stay where they are. Cloudflare's own cookies are
never carried: a passed check belongs to the machine that passed it.
"""
from __future__ import annotations

import argparse
import json
import sys

CDP_DEFAULT = "http://127.0.0.1:9222"
SESSION_COOKIE = "_circle_session"
CLOUDFLARE_COOKIES = frozenset({
    "cf_clearance", "__cf_bm", "_cfuvid", "__cflb", "__cfruid", "__cfseq", "__cf_ob",
})


def is_cloudflare(cookie: dict) -> bool:
    return cookie["name"] in CLOUDFLARE_COOKIES or cookie["name"].startswith("cf_chl")


def _bare(domain: str) -> str:
    return domain.lstrip(".").lower()


def circle_cookies(cookies: list[dict]) -> list[dict]:
    """The cookies of the bot's Circle sign-in, without Cloudflare's."""
    hosts = {_bare(c["domain"]) for c in cookies if c["name"] == SESSION_COOKIE}

    def related(cookie: dict) -> bool:
        d = _bare(cookie["domain"])
        if d == "circle.so" or d.endswith(".circle.so"):
            return True
        return any(d == h or h.endswith("." + d) or d.endswith("." + h) for h in hosts)

    return [c for c in cookies if related(c) and not is_cloudflare(c)]


def summary(cookies: list[dict]) -> str:
    sessions = {_bare(c["domain"]) for c in cookies if c["name"] == SESSION_COOKIE}
    central = "yes" if "login.circle.so" in sessions else "NO"
    return (f"{len(cookies)} cookies, {len(sessions)} hosts with a session, "
            f"central login session: {central}")


def _context(playwright, cdp: str):
    # The browser's own default context: the profile the bot signs in with.
    # The browser is never closed here; it belongs to its service or to a person.
    return playwright.chromium.connect_over_cdp(cdp).contexts[0]


def export(cdp: str) -> int:
    if sys.stdout.isatty():
        print("export writes the bot's sign-in to stdout; pipe it into `import`, "
              "never into a terminal or a file", file=sys.stderr)
        return 2
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        carried = circle_cookies(_context(p, cdp).cookies())
    if not any(_bare(c["domain"]) == "login.circle.so" and c["name"] == SESSION_COOKIE
               for c in carried):
        print("this browser holds no central login session (login.circle.so); "
              "sign the bot in there first", file=sys.stderr)
        return 1
    json.dump(carried, sys.stdout)
    print(f"exported {summary(carried)}", file=sys.stderr)
    return 0


def import_(cdp: str) -> int:
    from playwright.sync_api import sync_playwright

    cookies = circle_cookies(json.load(sys.stdin))
    with sync_playwright() as p:
        context = _context(p, cdp)
        context.add_cookies(cookies)
        held = {(c["name"], c["domain"], c["path"]) for c in context.cookies()}
    missing = [c for c in cookies if (c["name"], c["domain"], c["path"]) not in held]
    print(f"imported {summary(cookies)}; not taken by the browser: {len(missing)}")
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("direction", choices=("export", "import"))
    parser.add_argument("--cdp", default=CDP_DEFAULT, help=f"browser CDP address ({CDP_DEFAULT})")
    args = parser.parse_args(argv)
    return export(args.cdp) if args.direction == "export" else import_(args.cdp)


if __name__ == "__main__":
    sys.exit(main())
