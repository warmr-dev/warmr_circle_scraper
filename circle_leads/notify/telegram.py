"""Send a message to a Telegram chat, and never let that break the caller.

Rules this follows, learned from what alerting normally gets wrong:

- **Silence is the default.** With no token or chat id configured, every call
  is a no-op that returns False. A worker must run the same way on a laptop
  with no bot as it does on the server.
- **A failed send is never raised.** The alert is the least important thing in
  any code path it appears in; a Telegram outage must not stop a harvest or
  leave a lead unsent.
- **The same message is not repeated.** A community that fails every two
  minutes would otherwise send 720 identical messages a day, and the one that
  matters would be lost among them.
- **The rate limit is ours, not Telegram's.** Telegram allows about 30
  messages a second to different chats but throttles a single chat near 20 a
  minute; past that it starts returning 429 with a retry delay.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram rejects anything longer; cutting it ourselves keeps the useful part.
MAX_LEN = 4096
# One chat tolerates roughly 20 messages a minute before it answers 429.
MAX_PER_MINUTE = 18
# How long an identical message is suppressed.
DEDUP_WINDOW_S = 900.0

_lock = threading.Lock()
_sent_at: list[float] = []
_last_seen: dict[str, float] = {}


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)


def load_config() -> TelegramConfig:
    return TelegramConfig(
        token=(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
        chat_id=(os.environ.get("TELEGRAM_CHAT_ID") or "").strip(),
    )


def escape(text: str) -> str:
    """Escape the four characters Telegram's HTML parse mode cares about."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _allowed(now: float) -> bool:
    global _sent_at
    _sent_at = [t for t in _sent_at if t > now - 60]
    if len(_sent_at) >= MAX_PER_MINUTE:
        return False
    _sent_at.append(now)
    return True


def send(
    text: str,
    *,
    config: TelegramConfig | None = None,
    dedup_key: str | None = None,
    silent: bool = False,
    timeout: float = 10.0,
) -> bool:
    """Deliver one message. Returns whether it actually went out.

    ``dedup_key`` suppresses repeats of the same thing; it defaults to the
    message text, so a failure that recurs every cycle is reported once every
    ``DEDUP_WINDOW_S`` rather than every cycle.
    """
    cfg = config or load_config()
    if not cfg.enabled:
        return False
    if not (text or "").strip():
        return False

    now = time.monotonic()
    key = dedup_key if dedup_key is not None else text
    with _lock:
        last = _last_seen.get(key)
        if last is not None and now - last < DEDUP_WINDOW_S:
            return False
        if not _allowed(now):
            logger.warning("telegram: over the per-minute cap, dropping a message")
            return False
        _last_seen[key] = now
        # Keep the dedup table from growing without bound in a long-lived
        # process: anything older than the window can never suppress again.
        if len(_last_seen) > 500:
            for k, t in list(_last_seen.items()):
                if now - t > DEDUP_WINDOW_S:
                    _last_seen.pop(k, None)

    body = text if len(text) <= MAX_LEN else text[: MAX_LEN - 1] + "…"
    try:
        resp = requests.post(
            API.format(token=cfg.token),
            json={
                "chat_id": cfg.chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        # Deliberately not raised: the alert is the least important thing in
        # whatever code path called it.
        logger.warning("telegram: send failed (%s)", type(exc).__name__)
        return False

    if resp.status_code != 200:
        logger.warning("telegram: HTTP %s %s", resp.status_code, resp.text[:200])
        return False
    return True


def notify(title: str, body: str = "", *, level: str = "info", **kwargs) -> bool:
    """A titled message. ``level`` only picks the leading mark."""
    mark = {"info": "•", "success": "✓", "warning": "!", "error": "✗"}.get(level, "•")
    text = f"{mark} <b>{escape(title)}</b>"
    if body:
        text += "\n" + body
    return send(text, **kwargs)
