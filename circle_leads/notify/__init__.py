"""Telling a human something happened.

One channel for now: a Telegram bot. Everything here is best-effort -- a
notification that cannot be delivered must never take down the thing it was
reporting on.
"""

from circle_leads.notify.telegram import (
    TelegramConfig,
    escape,
    load_config,
    notify,
    send,
)

__all__ = ["TelegramConfig", "escape", "load_config", "notify", "send"]
