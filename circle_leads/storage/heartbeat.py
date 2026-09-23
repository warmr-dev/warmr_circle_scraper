"""A long-running service saying it is still alive.

Written as a thread on purpose. The worker's first heartbeat lived at the top
of its main loop, which is only reached between jobs -- and a single job here
can be a harvest that runs for hours. The service was working the whole time
(LLM calls every few seconds in the journal) while the dashboard's watchdog
declared it dead and sent an alert. A false alarm is worse than no alarm: it
teaches the person to ignore the channel that is supposed to wake them.

So the beat runs beside the work rather than inside it. It only stops being
written when the process is gone, which is exactly the thing the watchdog is
asking about.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from circle_leads.storage.settings_store import set_setting

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 60.0


def start_heartbeat(db, key: str, *, interval_s: float = DEFAULT_INTERVAL_S) -> Callable[[], None]:
    """Write ``key`` every ``interval_s`` until the returned callable is used.

    The thread is a daemon: a heartbeat must never be the reason a service
    refuses to exit. Database trouble is logged once per occurrence and
    otherwise ignored -- a missed beat is not a crash, and the watchdog's own
    limit is many multiples of the interval.
    """
    stop = threading.Event()

    def beat() -> None:
        while True:
            try:
                set_setting(db, key, datetime.utcnow().isoformat())
            except Exception as exc:  # noqa: BLE001 - never take the service down
                logger.warning("heartbeat %s failed (%s)", key, type(exc).__name__)
            # Wait on the event, not on sleep: a stop should be immediate.
            if stop.wait(interval_s):
                return

    thread = threading.Thread(target=beat, name=f"heartbeat-{key}", daemon=True)
    thread.start()

    def cancel() -> None:
        stop.set()
        thread.join(timeout=5)

    return cancel
