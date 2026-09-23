"""Fast feed polling: see a new post within a couple of minutes, not hours.

The harvest walks every space of every community and takes hours, so a post
published a minute after it passed waits for the next run. This reads one page
of each community's newest-first feed instead, every couple of minutes, and
hands anything new to the same triage the harvest uses.
"""

from circle_leads.watch.poller import (
    WatchOutcome,
    check_community,
    ensure_watch_rows,
    run_watch,
)

__all__ = ["WatchOutcome", "check_community", "ensure_watch_rows", "run_watch"]
