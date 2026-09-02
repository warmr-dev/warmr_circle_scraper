"""Draft an opening reply for a lead.

The draft is a starting point for a human, never something to send
automatically. It is deliberately plain: it names what the person asked for,
says what you can do, and asks one question. No flattery, no pitch deck, no
invented claims about your experience -- you fill that in.

Community norms usually favour replying in the original thread over a DM.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_QUOTE_CHARS = 120


@dataclass
class ReplyDraft:
    text: str
    channel: str = "reply_in_thread"
    notes: list[str] | None = None


def _shorten(text: str, limit: int = MAX_QUOTE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _what_they_need(lead: dict) -> str:
    """Describe the ask in the poster's own terms, not ours."""
    title = lead.get("job_title")
    skills = lead.get("skills") or []
    target = lead.get("hire_target")

    if title:
        title = str(title).lower()
        return title if title.startswith(("a ", "an ")) else f"a {title}"
    if skills:
        return f"{skills[0]} work"
    if target and target != "individual developer":
        return f"a {target}" if not str(target).startswith(("a ", "an ")) else str(target)
    return "what you described"


def draft_reply(lead: dict, *, your_name: str | None = None) -> ReplyDraft:
    """Compose an opening message for one lead."""
    need = _what_they_need(lead)
    quote = _shorten(lead.get("evidence_quote") or lead.get("content") or "")
    skills = lead.get("skills") or []
    urgency = (lead.get("urgency") or "").lower()
    budget = lead.get("budget")
    notes: list[str] = []

    lines: list[str] = []

    opener = f"Hi{' ' + lead['author'].split()[0] if lead.get('author') else ''} —"
    lines.append(f"{opener} saw you're looking for {need}.")

    if skills:
        lines.append(
            f"I work with {', '.join(skills[:3])} and have built and shipped "
            "this kind of thing before."
        )
    else:
        lines.append("This is the kind of work I do.")

    # One concrete question, chosen from what the post left unsaid, so the
    # reply invites a real answer rather than a yes/no.
    if not budget and urgency in ("high", "medium"):
        question = "What's your timeline, and is there a budget range in mind?"
    elif not budget:
        question = "What does the scope look like, and do you have a budget range?"
    elif urgency == "high":
        question = "How soon do you need someone starting?"
    else:
        question = "What does the scope look like at the moment?"
    lines.append(question)

    if your_name:
        lines.append(f"\n— {your_name}")

    if urgency == "high":
        notes.append("Marked urgent — reply soon or the moment passes.")
    if lead.get("hire_target") == "software agency":
        notes.append("They asked for an agency; say so if you're an individual.")
    if lead.get("is_duplicate"):
        notes.append("Near-duplicate of another lead — check you haven't replied already.")
    if (lead.get("confidence") or 0) < 0.8:
        notes.append("Lower-confidence classification — read the thread before replying.")

    return ReplyDraft(
        text="\n".join(lines),
        channel="reply_in_thread",
        notes=notes or None,
    )
