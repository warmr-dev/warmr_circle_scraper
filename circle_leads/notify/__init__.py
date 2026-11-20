"""Telling a human something happened.

One channel for now: a Telegram bot. Everything here is best-effort -- a
notification that cannot be delivered must never take down the thing it was
reporting on.
"""

from circle_leads.notify.cookies import report_cookie_death
from circle_leads.notify.telegram import (
    TelegramConfig,
    cookie_problem,
    escape,
    load_config,
    notify,
    send,
)

__all__ = [
    "TelegramConfig",
    "cookie_problem",
    "escape",
    "load_config",
    "notify",
    "report_cookie_death",
    "send",
]
