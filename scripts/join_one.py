"""Join one community with the script in the lead: measure where the agent is
actually needed.

Runs the known path in Python (classify, sign in, 2FA code from the bot
mailbox, join gate, membership check) against a browser that is already
running with --remote-debugging-port. It stops at the first state the code is
not allowed to decide -- a profile form, or anything UNKNOWN -- and prints what
the agent would have to be called for.

Read-only against the database: it prints, it does not persist.

    python scripts/join_one.py https://www.generouslifeapp.com
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from circle_leads.join import cdp_driver as d
from circle_leads.join.email_code import fetch_login_code


def _step(name: str, started: float) -> None:
    print(f"  [{time.time() - started:5.1f}s] {name}", flush=True)


def main(url: str) -> int:
    host = urlparse(url).hostname or ""
    email = os.environ["CIRCLE_EMAIL4"]
    password = os.environ["CIRCLE_PASSWORD4"]
    started = time.time()
    print(f"target {url}")

    with d.attach() as page:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        state, member = d.assess(page)
        _step(f"state={state} member={member.is_member} spaces={member.count}", started)

        if state == d.MEMBER:
            print("already a member -- nothing to do")
            return 0

        if state in (d.LOGIN_WALL, d.JOIN_GATE, d.UNKNOWN):
            login_started = datetime.now(timezone.utc)
            state = d.sign_in(page, email=email, password=password, community_host=host)
            _step(f"after sign_in: state={state}", started)

            if state == d.EXTERNAL_LOGIN:
                print("RESULT external_login -- community runs its own login, hand to a human")
                return 2

            if state == d.TWO_FA or "/two_fa" in page.url:
                print("  waiting for the emailed code (up to 5 min)...", flush=True)
                code = fetch_login_code(
                    email, since=login_started - timedelta(seconds=30), timeout_seconds=300
                )
                if not code:
                    print("RESULT needs_email_code -- no code arrived")
                    return 3
                _step(f"code {code} fetched", started)
                state = d.enter_code(page, code)
                _step(f"after code: state={state}", started)

        state, member = d.assess(page)
        _step(f"state={state} member={member.is_member}", started)

        if state == d.JOIN_GATE:
            state = d.click_join(page)
            _step(f"after join click: state={state}", started)
            state, member = d.assess(page)

        if member.is_member:
            print(f"RESULT joined -- flags={member.flags[:6]} url={page.url}")
            return 0
        if state == d.PROFILE_STEP:
            print(f"RESULT agent_needed: profile form at {page.url}")
            return 10
        print(f"RESULT agent_needed: state={state} url={page.url}")
        return 10


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
