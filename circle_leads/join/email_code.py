"""Read the 6-digit code Circle mails when a login has to be verified.

Circle asks for a code whenever an account signs in from a browser profile it
has not seen before -- so on every fresh profile: a server, a rebuilt
container, a new Ego profile. Measured 2026-09-25: a first login in a clean
Chrome profile went straight to ``/two_fa``. Unattended runs have nobody to
type that code, which is where the join bot stops.

Deliberately narrow. The mailbox is opened read-only (``BODY.PEEK``, no flags
changed, nothing deleted, nothing sent), only mail addressed to the bot account
and newer than the moment the login started is looked at, and only the digits
come back. No model ever sees the messages: an inbox is a channel anyone can
write to, so letting an agent read it is an instruction-injection surface.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

DEFAULT_IMAP_HOST = "imap.gmail.com"
# Circle's code is six digits; so are plenty of other numbers in an email.
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# Words Circle puts next to the code, used to pick the right number when a
# message carries several.
_CONTEXT_WORDS = ("code", "verification", "verify", "confirm", "one-time")
_CONTEXT_WINDOW = 60


class EmailCodeError(RuntimeError):
    """The mailbox itself is unusable: no credentials, or IMAP refused us."""


def _credentials(user: str | None, password: str | None) -> tuple[str, str]:
    user = user or os.environ.get("CIRCLE_MAIL_USER", "")
    password = password or os.environ.get("CIRCLE_MAIL_APP_PASSWORD", "")
    if not user or not password:
        raise EmailCodeError(
            "CIRCLE_MAIL_USER and CIRCLE_MAIL_APP_PASSWORD (a Gmail app password, "
            "not the account password) must be set -- see .env"
        )
    return user, password


def _plain_text(message: Message) -> str:
    """The message body as text; HTML tags stripped just enough to read digits."""
    parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() != "text":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # pragma: no cover -- malformed MIME
            continue
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if part.get_content_subtype() == "html":
            text = re.sub(r"<[^>]+>", " ", text)
        parts.append(text)
    return "\n".join(parts)


def _best_code(text: str) -> str | None:
    """A 6-digit code from ``text``: the group closest to a word like "code",
    or the first group when the text has no such word.

    Distance decides, not mere presence in a window -- "Order 998877 shipped.
    Your verification code is 314159." puts both numbers near the phrase, and
    only the nearer one is the code. A word *before* the digits counts double,
    because that is the shape these mails use.
    """
    matches = list(_CODE_RE.finditer(text))
    if not matches:
        return None
    lowered = text.lower()
    anchors = [
        (m.start(), m.end())
        for word in _CONTEXT_WORDS
        for m in re.finditer(re.escape(word), lowered)
    ]
    if not anchors:
        return matches[0].group(1)

    def distance(match: re.Match[str]) -> float:
        best = float("inf")
        for start, end in anchors:
            if end <= match.start():  # the word introduces the digits
                gap = match.start() - end
            elif start >= match.end():  # the word follows them
                gap = (start - match.end()) * 2
            else:  # overlapping, e.g. digits inside the word
                gap = 0
            best = min(best, gap)
        return best

    best_match = min(matches, key=lambda m: (distance(m), m.start()))
    if distance(best_match) > _CONTEXT_WINDOW:
        return matches[0].group(1)
    return best_match.group(1)


def extract_code(message: Message) -> str | None:
    """The verification code in one message, or None if it carries none."""
    subject = str(message.get("Subject", ""))
    return _best_code(subject) or _best_code(_plain_text(message))


def message_sent_at(message: Message) -> datetime | None:
    """The message's Date as an aware UTC datetime, or None if unparsable."""
    raw = message.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_login_code(
    recipient: str,
    *,
    since: datetime,
    timeout_seconds: float = 120.0,
    poll_seconds: float = 5.0,
    host: str | None = None,
    user: str | None = None,
    password: str | None = None,
    imap_factory=imaplib.IMAP4_SSL,
    sleep=time.sleep,
    now=lambda: datetime.now(timezone.utc),
) -> str | None:
    """Wait for Circle's code addressed to ``recipient`` and return it.

    ``since`` is the moment the login started: anything older is a code from an
    earlier run and is ignored, which is what stops a stale code from being
    typed into a fresh challenge. Returns None if nothing arrives before
    ``timeout_seconds``; the caller then hands the community to a human.

    The connection is opened per call and closed again -- a join attempt lasts
    minutes, and a held IMAP connection is one more thing to time out.
    """
    user, password = _credentials(user, password)
    # Gmail is only the default: a bot mailbox moves to another provider the
    # day Google refuses an app password, and that must not need a code change.
    host = host or os.environ.get("CIRCLE_MAIL_IMAP_HOST") or DEFAULT_IMAP_HOST
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    deadline = now() + timedelta(seconds=timeout_seconds)
    # IMAP SINCE has day granularity; the Date header does the precise cut.
    search_since = (since - timedelta(days=1)).strftime("%d-%b-%Y")

    while True:
        code = _poll_once(
            recipient,
            since=since,
            search_since=search_since,
            host=host,
            user=user,
            password=password,
            imap_factory=imap_factory,
        )
        if code:
            return code
        if now() >= deadline:
            logger.info("no verification code for %s within %ss", recipient, timeout_seconds)
            return None
        sleep(poll_seconds)


def _poll_once(
    recipient: str,
    *,
    since: datetime,
    search_since: str,
    host: str,
    user: str,
    password: str,
    imap_factory,
) -> str | None:
    try:
        client = imap_factory(host)
    except OSError as exc:
        raise EmailCodeError(f"cannot reach {host}: {exc}") from exc
    try:
        try:
            client.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise EmailCodeError(
                f"IMAP login failed for {user} -- is CIRCLE_MAIL_APP_PASSWORD an app password? ({exc})"
            ) from exc
        # readonly: the bot must not mark the operator's mail as read.
        client.select("INBOX", readonly=True)
        status, data = client.search(None, "TO", f'"{recipient}"', "SINCE", search_since)
        if status != "OK" or not data or not data[0]:
            return None
        # Newest first: the code we want is the last one Circle sent.
        for uid in reversed(data[0].split()[-20:]):
            status, payload = client.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw = next(
                (part[1] for part in payload if isinstance(part, tuple) and part[1]),
                None,
            )
            if not raw:
                continue
            message = email.message_from_bytes(raw)
            sent_at = message_sent_at(message)
            if sent_at is not None and sent_at < since:
                continue
            code = extract_code(message)
            if code:
                return code
        return None
    finally:
        try:
            client.logout()
        except Exception:  # pragma: no cover -- best effort
            pass
