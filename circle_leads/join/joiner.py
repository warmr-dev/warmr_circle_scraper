"""Auto-join batch orchestration: pick candidates, drive ego-browser, persist
outcomes.

Deliberately not the VPS+Playwright design from the original pivot plan --
ego-browser drives the operator's own Mac browser instead of a headless bot
with credentials baked into a server deployment. It reuses the operator's
existing CIRCLE_EMAIL/CIRCLE_PASSWORD (already the connector's account) only
to log in on a host ego-browser isn't already authenticated on -- a
*.circle.so login doesn't carry over to a custom domain, confirmed live. It
automates the part that needs no judgment call (navigate, log in if needed,
click Join, classify a known outcome); anything ambiguous -- an
unrecognized login form, a Cloudflare challenge, an unrecognized page state,
or a custom application form -- stops the batch and hands the ego-browser
task space back to the operator instead of guessing. See
``ego_join_driver.mjs`` for the exact state machine.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from circle_leads.config.settings import JoinPacingConfig
from circle_leads.join.ego_bridge import EgoBrowserError, attempt_join, open_join_space
from circle_leads.join.pacing import joins_attempted_today, sleep_between_attempts
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community, JoinStatus, utcnow
from circle_leads.web.replay_store import connect_host

logger = logging.getLogger(__name__)

# "main" is the connector's existing account (CIRCLE_EMAIL/PASSWORD, already
# used to actually source leads); "test" is a separate account for anything
# that shouldn't touch main's Circle-side reputation -- spam-looking
# communities, and exercising new driver behavior. Circle rate-limits/flags
# per account, so this is a real second budget, not just a label -- see the
# account-scoped cap below.
_ACCOUNT_ENV_VARS = {
    "main": ("CIRCLE_EMAIL", "CIRCLE_PASSWORD"),
    "test": ("CIRCLE_EMAIL2", "CIRCLE_PASSWORD2"),
}

# Every raw driver outcome, terminal or not -- _persist_terminal_outcome only
# ever writes joined/paid_skip/pending_approval/etc. to the DB, so a handoff
# like application_form_detected (an unrecognized custom application form)
# used to vanish the moment the process exited, with no record it ever
# happened. This is the only durable trace of "what UI/form variants has the
# batch actually seen" across runs.
ATTEMPT_LOG_PATH = Path("data/join_attempts.log")


def _log_attempt(candidate: dict, status: str, detail: str, account: str) -> None:
    ATTEMPT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ATTEMPT_LOG_PATH.open("a") as f:
        f.write(json.dumps({
            "at": utcnow().isoformat(),
            "account": account,
            "slug": candidate["slug"],
            "url": candidate["url"],
            "status": status,
            "detail": detail,
        }) + "\n")


def _attempts_today_for_account(account: str) -> int:
    """Daily-cap count for any account other than "main": the DB's
    join_attempted_at column doesn't record which account made an attempt (it
    predates having more than one), so it can't isolate a second account's
    count -- only this log (written for every attempt, from the point this
    account started being used) can. "main" deliberately keeps using the
    DB-wide count instead (see run_auto_join) since that reflects its whole
    real history, not just what's been logged since today's log file existed.
    """
    if not ATTEMPT_LOG_PATH.exists():
        return 0
    today = utcnow().date().isoformat()
    count = 0
    for line in ATTEMPT_LOG_PATH.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (
            entry.get("account") == account
            and str(entry.get("at", "")).startswith(today)
            and entry.get("status") in _TERMINAL_STATUS_MAP
        ):
            count += 1
    return count

# ego-browser outcome -> Community.join_status: only these are ever persisted.
# Everything else (needs_login, challenge_stop, application_form_detected,
# unclear) means the driver stopped without a definitive result -- the
# community is left `not_attempted` so it's retried once the blocker (login,
# a form answer, ...) is resolved, instead of being wrongly recorded as failed.
_TERMINAL_STATUS_MAP = {
    "joined": JoinStatus.JOINED.value,
    "paid_skip": JoinStatus.PAID_SKIP.value,
    "pending_approval": JoinStatus.PENDING_APPROVAL.value,
    "subscription_expired_skip": JoinStatus.SUBSCRIPTION_EXPIRED_SKIP.value,
    "invite_skip": JoinStatus.INVITE_SKIP.value,
}


@dataclass
class JoinBatchResult:
    space_id: int
    attempted: list[str] = field(default_factory=list)
    joined: list[str] = field(default_factory=list)
    stopped_for: str | None = None  # candidate slug that triggered a handoff/error, if any
    stop_reason: str | None = None


def select_join_candidates(db: Database, *, limit: int | None = None) -> list[dict]:
    """Communities ready for a join attempt: ICP-fit, on Circle, joinable,
    never attempted -- the same gate ``pipeline.classify_icp_pending`` and the
    dashboard's Join Activity backlog count document."""
    with db.session() as s:
        query = (
            select(
                Community.id, Community.slug, Community.url, Community.name,
                Community.join_type, Community.icp_score,
            )
            .where(
                Community.icp_flag.is_(True),
                Community.platform == "circle",
                Community.join_type.in_(["free_join", "paid"]),
                Community.join_status == JoinStatus.NOT_ATTEMPTED.value,
            )
            .order_by(Community.icp_score.desc())
        )
        if limit:
            query = query.limit(limit)
        rows = s.execute(query).all()
    return [
        {
            "id": r.id, "slug": r.slug, "url": r.url, "name": r.name,
            "join_type": r.join_type, "icp_score": r.icp_score,
        }
        for r in rows
    ]


def _persist_terminal_outcome(db: Database, community_id: int, status: str, detail: str) -> None:
    with db.session() as s:
        community = s.get(Community, community_id)
        if community is None:
            return
        community.join_status = status
        community.join_status_detail = detail[:2000]
        community.join_attempted_at = utcnow()
        community.join_attempts += 1
        if status == JoinStatus.JOINED.value:
            community.joined_at = utcnow()


def run_auto_join(
    db: Database,
    *,
    limit: int | None = None,
    host: str | None = None,
    pacing: JoinPacingConfig | None = None,
    space_id: int | None = None,
    screenshot_dir: str | None = None,
    dry_run: bool = False,
    account: str = "main",
) -> JoinBatchResult:
    """Attempt to join every selected candidate in order, stopping at the
    first one that needs a human (or on a daily-cap/bridge error)."""
    if account not in _ACCOUNT_ENV_VARS:
        raise ValueError(f"Unknown account {account!r} -- expected one of {sorted(_ACCOUNT_ENV_VARS)}.")
    pacing = pacing or JoinPacingConfig()
    candidates = select_join_candidates(db, limit=limit)
    if host:
        # An exact slug match wins outright: a short/generic slug (e.g. "s")
        # would otherwise substring-match almost every candidate's URL (they
        # all contain "https"), silently sweeping in unrelated communities.
        exact = [c for c in candidates if c["slug"] == host]
        candidates = exact if exact else [c for c in candidates if host in (c["url"] or "")]

    if dry_run:
        return JoinBatchResult(space_id=space_id or 0, attempted=[c["slug"] for c in candidates])
    if not candidates:
        return JoinBatchResult(space_id=space_id or 0)

    already_today = joins_attempted_today(db) if account == "main" else _attempts_today_for_account(account)
    if already_today >= pacing.max_joins_per_day:
        raise RuntimeError(
            f"Daily join cap already reached for account {account!r} "
            f"({already_today}/{pacing.max_joins_per_day}) -- "
            "try again tomorrow or raise join_pacing.max_joins_per_day."
        )

    # Reused as this account's Circle login for hosts ego-browser isn't
    # already authenticated on (a *.circle.so login doesn't carry over to a
    # custom domain -- confirmed live).
    email_var, password_var = _ACCOUNT_ENV_VARS[account]
    email = os.environ.get(email_var)
    password = os.environ.get(password_var)

    sid = space_id if space_id is not None else open_join_space(f"warmr auto-join ({account})")
    result = JoinBatchResult(space_id=sid)

    for candidate in candidates:
        if already_today >= pacing.max_joins_per_day:
            result.stop_reason = "daily join cap reached mid-batch"
            break

        try:
            outcome = attempt_join(
                sid, candidate["url"], email=email, password=password, screenshot_dir=screenshot_dir
            )
        except EgoBrowserError as exc:
            logger.error("ego-browser bridge failed on %s: %s", candidate["slug"], exc)
            result.stopped_for = candidate["slug"]
            result.stop_reason = f"ego-browser bridge error: {exc}"
            break

        result.attempted.append(candidate["slug"])
        _log_attempt(candidate, outcome.status, outcome.detail, account)

        if outcome.status not in _TERMINAL_STATUS_MAP:
            result.stopped_for = candidate["slug"]
            result.stop_reason = f"{outcome.status}: {outcome.detail}"
            break

        _persist_terminal_outcome(db, candidate["id"], _TERMINAL_STATUS_MAP[outcome.status], outcome.detail)
        # Every terminal status counts toward the cap, not just "joined" --
        # paid_skip/subscription_expired_skip/invite_skip/pending_approval all
        # set join_attempted_at too, so they all count against
        # joins_attempted_today() on the next fresh process. Only bumping this
        # in-memory counter on "joined" would let one long-running batch blow
        # past the real cap before its own stale count noticed.
        already_today += 1
        if outcome.status == "joined":
            result.joined.append(candidate["slug"])
            if outcome.cookies:
                # The cookies' own domain is the page's *actual* final host --
                # more trustworthy than candidate["url"], which can be a
                # discover.circle.so listing page that redirected elsewhere.
                # This is the entire join->scrape hookup: without it, a real
                # join never reaches scan_cookie_host()/cookie_hosts_vip_first().
                host = outcome.cookies[0]["domain"]
                connect_host(db, host, outcome.cookies, member_label=candidate["name"])
            else:
                logger.warning(
                    "joined %s but captured no session cookies -- won't be scraped "
                    "until replay_store.connect_host() is called for it manually",
                    candidate["slug"],
                )

        sleep_between_attempts(pacing)

    return result
