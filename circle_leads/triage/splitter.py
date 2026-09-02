"""Split pasted community text into individual posts.

You read a community you have joined, copy what is on screen, and paste it
here. Browser copy loses all structure, so this reconstructs post boundaries
from the shapes that survive: blank-line gaps, explicit separators, and the
"Name · 2h ago" byline Circle renders above each post.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# An explicit separator the user can type to remove all ambiguity.
EXPLICIT_SEPARATOR = re.compile(r"^\s*(?:---+|===+|\*\*\*+|###+)\s*$", re.M)

# "Dana Ops · 2h ago", "Sam Dev - Sep 2", "Priya — yesterday": a display name
# followed by a separator and a relative or absolute time. This is the most
# reliable structural signal Circle's UI leaves in copied text.
BYLINE = re.compile(
    r"^[ \t]*"
    r"(?P<author>[A-Z][\w'’.-]*(?:[ \t]+[A-Z][\w'’.-]*){0,3})"
    r"[ \t]*[·•\-–—|][ \t]*"
    r"(?P<when>"
    r"\d+\s*(?:s|m|h|d|w|mo|y|sec|min|hour|day|week|month|year)s?\s*(?:ago)?"
    r"|just\s+now|yesterday|today"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}"
    r"|\d{4}-\d{2}-\d{2}"
    r")\b.*$",
    re.M | re.I,
)

# Engagement chrome Circle renders around posts; it is not content.
NOISE_LINES = re.compile(
    r"^[ \t]*(?:"
    # "12 likes", "4 comments", and any run of them on one line
    r"(?:\d+\s*(?:likes?|comments?|replies|repl(?:y|ies)|views?|members?)[ \t]*)+"
    r"|(?:like|reply|share|comment|follow|report|copy\s+link|view\s+\d+\s+repl\w*)"
    r"|·|•|\.{3}|…"
    r")[ \t]*$",
    re.M | re.I,
)

MIN_POST_CHARS = 15


@dataclass
class RawPost:
    """One post recovered from pasted text."""

    content: str
    author: str | None = None
    posted_label: str | None = None
    index: int = 0
    meta: dict = field(default_factory=dict)


def _clean(text: str) -> str:
    text = NOISE_LINES.sub("", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_on_bylines(text: str) -> list[RawPost] | None:
    """Split where Circle's "Name · time" bylines appear.

    Returns None when there are too few bylines to be a reliable signal, so
    the caller can fall back to blank-line splitting.
    """
    matches = list(BYLINE.finditer(text))
    if len(matches) < 2:
        return None

    posts: list[RawPost] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _clean(text[start:end])
        if len(body) < MIN_POST_CHARS:
            continue
        posts.append(
            RawPost(
                content=body,
                author=m.group("author").strip(),
                posted_label=m.group("when").strip(),
                index=len(posts),
            )
        )
    return posts or None


def _split_on_blank_lines(text: str) -> list[RawPost]:
    """Fall back to paragraph blocks separated by blank lines."""
    posts: list[RawPost] = []
    for block in re.split(r"\n\s*\n", text):
        body = _clean(block)
        if len(body) < MIN_POST_CHARS:
            continue
        author = posted = None
        # A byline may still lead the block even if it was too sparse to split on.
        lead = BYLINE.match(body)
        if lead:
            author = lead.group("author").strip()
            posted = lead.group("when").strip()
            body = _clean(body[lead.end():])
            if len(body) < MIN_POST_CHARS:
                continue
        posts.append(
            RawPost(content=body, author=author, posted_label=posted, index=len(posts))
        )
    return posts


def split_posts(text: str) -> list[RawPost]:
    """Recover individual posts from a block of pasted community text.

    Precedence: an explicit separator the user typed, then Circle's bylines,
    then blank-line blocks. Whole input as one post if nothing else applies.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []

    if EXPLICIT_SEPARATOR.search(text):
        posts: list[RawPost] = []
        for chunk in EXPLICIT_SEPARATOR.split(text):
            for p in _split_on_blank_lines(chunk) or []:
                p.index = len(posts)
                posts.append(p)
        if posts:
            return posts

    by_byline = _split_on_bylines(text)
    if by_byline:
        return by_byline

    blocks = _split_on_blank_lines(text)
    if blocks:
        return blocks

    body = _clean(text)
    return [RawPost(content=body)] if len(body) >= MIN_POST_CHARS else []
