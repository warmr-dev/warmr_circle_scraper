#!/usr/bin/env python
"""Tell a human that a systemd unit stopped.

Run by ``warmr-alert@.service`` through ``OnFailure=``. It lives in a file
rather than inside the unit because a multi-line ``python -c`` in a unit file
is joined with its leading indentation and dies on an IndentationError -- which
is what the first version of this did, silently, until it was fired by hand.

The alert deliberately comes from outside the failing process: by the time a
service has crashed, nothing inside it can still report anything.
"""

from __future__ import annotations

import subprocess
import sys

from circle_leads.notify import notify

TAIL_LINES = 15
# Telegram cuts at 4096; leave room for the title and the <pre> wrapper.
MAX_LOG_CHARS = 1500


def journal_tail(unit: str) -> str:
    try:
        done = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(TAIL_LINES), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(journal unavailable: {type(exc).__name__})"
    return (done.stdout or done.stderr or "").strip()[-MAX_LOG_CHARS:]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: alert_unit_failed.py <unit>", file=sys.stderr)
        return 2
    unit = argv[1]
    log = journal_tail(unit) or "(no log lines)"
    body = "Последние строки журнала:\n<pre>" + (
        log.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ) + "</pre>"
    sent = notify(
        f"Warmr: служба {unit} упала",
        body,
        level="error",
        dedup_key=f"unit-failed:{unit}",
    )
    # A unit that cannot reach Telegram should still be visible in the journal.
    print(f"alert for {unit}: {'sent' if sent else 'not sent'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
