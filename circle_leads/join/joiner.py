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
or a custom application form -- is handed back to the operator instead of
guessed at. The batch skips past such a community and carries on: it used to
stop dead there, which let a single unresolvable host hold up the whole
queue run after run (see ``_handoff_counts``). See ``ego_join_driver.mjs``
for the exact state machine.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import or_, select

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


def _iter_attempt_log() -> "list[dict]":
    """Every parseable line of the attempt log, oldest first ([] if there is none)."""
    if not ATTEMPT_LOG_PATH.exists():
        return []
    entries = []
    for line in ATTEMPT_LOG_PATH.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _visits_today_for_account(account: str, entries: "list[dict] | None" = None) -> int:
    """Community pages this account opened in a browser today, from the attempt log.

    What the daily cap actually manages is how much activity Circle sees from
    one account in a day, not how many joins landed: WORKLOG 2026-09-16
    records ~94 community visits in one day producing a fresh /two_fa
    challenge on three hosts that had worked fine before. So every attempt
    counts here, terminal or not. Counting only what reached the DB is what
    let the 2026-09-16 run visit 70 hosts while counting 25, and finish the
    day at 29/25 -- the cap both breached and ineffective.

    The DB can't supply this number: join_attempted_at is only ever written
    for a terminal outcome, so it cannot see a handoff at all, and it carries
    no per-account marker (it predates having more than one account).

    ``entries`` is an already-parsed snapshot of the log (see
    ``run_auto_join``); without one the log is read and parsed here.
    """
    if entries is None:
        entries = _iter_attempt_log()
    today = utcnow().date().isoformat()
    return sum(
        1
        for entry in entries
        if entry.get("account") == account and str(entry.get("at", "")).startswith(today)
    )


def _attempts_today(db: Database, account: str, entries: "list[dict] | None" = None) -> int:
    """Today's activity for the cap, keeping the per-account split intact.

    "main" takes whichever of its two sources is larger: the DB-wide count is
    its whole real history (including attempts from before this log file
    existed, or from another process), the log is the only one that sees
    visits that never reached a terminal outcome. Neither can hide activity
    the other saw. Any other account has no DB footprint to read -- see
    _visits_today_for_account -- so the log is all there is.
    """
    visits = _visits_today_for_account(account, entries)
    if account == "main":
        return max(joins_attempted_today(db), visits)
    return visits


def _handoff_counts(entries: "list[dict] | None" = None) -> dict[str, int]:
    """How often each slug has ended an attempt needing a human, from the attempt log.

    Head-of-line blocking guard. select_join_candidates orders
    deterministically, so the community that needed a human was picked first
    again on the very next run, and one unresolvable host could stall the
    queue indefinitely -- WORKLOG 2026-09-16: a 109-host batch left 44 in
    handoff and 39 hosts the queue never reached. Counting handoffs here
    sorts those hosts behind everything untried without ever dropping them:
    they stay in the queue, visible in the log, and come back around
    least-stuck-first once the fresh backlog is exhausted.

    Deliberately not account-scoped: a handoff is nearly always a property of
    the host's own UI (a custom application form, a challenge, an
    unrecognized page state) rather than of the login that hit it.

    ``entries`` is an already-parsed snapshot of the log (see
    ``run_auto_join``); without one the log is read and parsed here.
    """
    if entries is None:
        entries = _iter_attempt_log()
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.get("status") in _TERMINAL_STATUS_MAP:
            continue
        slug = entry.get("slug")
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    return counts


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
    # slug -> "status: detail" for every candidate that needed a human. These
    # no longer end the batch, so there can be many of them in one run, and
    # the run keeps going past each.
    handoffs: dict[str, str] = field(default_factory=dict)
    stopped_for: str | None = None  # candidate slug that ended the batch early, if any
    stop_reason: str | None = None


def _match_host(candidates: list[dict], host: str) -> list[dict]:
    """The subset an operator meant by ``--host``.

    An exact slug match wins outright: a short/generic slug (e.g. "s") would
    otherwise substring-match almost every candidate's URL (they all contain
    "https"), silently sweeping in unrelated communities.
    """
    exact = [c for c in candidates if c["slug"] == host]
    return exact if exact else [c for c in candidates if host in (c["url"] or "")]


def select_join_candidates(
    db: Database,
    *,
    limit: int | None = None,
    host: str | None = None,
    attempt_log: "list[dict] | None" = None,
) -> list[dict]:
    """Communities ready for a join attempt: ICP-fit, on Circle, joinable,
    never attempted -- the same gate ``pipeline.classify_icp_pending`` and the
    dashboard's Join Activity backlog count document.

    ``host`` narrows the queue to one targeted community (slug, or a substring
    of its URL). It is applied *here*, before the limit, because it is an
    operator override: filtering the already-truncated list instead would
    return nothing at all for exactly the hosts an operator retries by name --
    a previously-stuck host is sorted to the very end of the queue (see
    ``_handoff_counts``), so it falls past any limit before the filter ever
    sees it.

    ``attempt_log`` is an already-parsed snapshot of the attempt log, so a
    caller that needs it more than once can pay for the parse only once.
    """
    handoffs = _handoff_counts(attempt_log)
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
        if host:
            # A superset of what _match_host keeps, so the exact-match-wins
            # refinement below still decides -- this only keeps SQL from
            # hauling the whole queue back for a single targeted host.
            query = query.where(or_(Community.slug == host, Community.url.like(f"%{host}%")))
        elif limit:
            # Bound what the DB materialises without defeating the reordering
            # below. The sort only ever demotes a row that has a handoff, so at
            # most len(handoffs) rows can be pushed below any given row: the
            # final top `limit` is therefore always inside the first
            # `limit + len(handoffs)` rows by icp_score. Cutting at plain
            # `limit` here instead is what would keep handing back the same
            # stuck head of the queue that the reordering exists to get past.
            query = query.limit(limit + len(handoffs))
        rows = s.execute(query).all()
    candidates = [
        {
            "id": r.id, "slug": r.slug, "url": r.url, "name": r.name,
            "join_type": r.join_type, "icp_score": r.icp_score,
        }
        for r in rows
    ]
    if host:
        candidates = _match_host(candidates, host)
    # Hosts that previously needed a human go last, fewest handoffs first (see
    # _handoff_counts). Python's sort is stable, so the SQL icp_score ordering
    # survives inside each group.
    candidates.sort(key=lambda c: handoffs.get(c["slug"], 0))
    if limit:
        candidates = candidates[:limit]
    return candidates


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
    """Attempt to join every selected candidate in order.

    A candidate that needs a human is recorded and skipped, not treated as
    the end of the batch -- only the daily cap or a broken ego-browser bridge
    stops the run. See ``_handoff_counts`` for why (one stuck host used to
    block the queue on every subsequent run too).

    ``host`` is an operator override and is honoured whatever the queue order
    says: a host that has needed a human before is sorted to the back of the
    queue, and naming it is precisely how the operator retries it. See
    ``select_join_candidates``.
    """
    if account not in _ACCOUNT_ENV_VARS:
        raise ValueError(f"Unknown account {account!r} -- expected one of {sorted(_ACCOUNT_ENV_VARS)}.")
    pacing = pacing or JoinPacingConfig()
    # One snapshot of the attempt log for the whole batch: the candidate
    # ordering (_handoff_counts) and the daily cap (_attempts_today) both read
    # it, and the file only ever grows, so re-parsing it per caller made every
    # run a little more expensive than the last for no new information. Both
    # readings used to happen before the first browser visit anyway, so this
    # cannot loosen the cap; anything appended mid-batch is counted in Python
    # below, exactly as before.
    attempt_log = _iter_attempt_log()
    candidates = select_join_candidates(db, limit=limit, host=host, attempt_log=attempt_log)

    if dry_run:
        return JoinBatchResult(space_id=space_id or 0, attempted=[c["slug"] for c in candidates])
    if not candidates:
        return JoinBatchResult(space_id=space_id or 0)

    already_today = _attempts_today(db, account, attempt_log)
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
            result.stop_reason = "daily cap reached mid-batch (every page opened counts)"
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
        # Counted here, before the outcome is even classified: the cap bounds
        # how much activity Circle sees from this account today, and it has
        # already seen this page load. Counting only what gets written to the
        # DB is what let the 2026-09-16 run visit 70 hosts on a cap of 25.
        already_today += 1

        if outcome.status not in _TERMINAL_STATUS_MAP:
            # Nothing is written to the DB (the community stays
            # `not_attempted` so it is retried once the blocker is resolved),
            # but the batch moves on to the next candidate instead of ending
            # here. The handoff is durable in the attempt log, which is also
            # what pushes this host down the queue on the next run.
            result.handoffs[candidate["slug"]] = f"{outcome.status}: {outcome.detail}"
            logger.info(
                "%s needs a human (%s) -- skipping to the next candidate",
                candidate["slug"], outcome.status,
            )
        else:
            _persist_terminal_outcome(
                db, candidate["id"], _TERMINAL_STATUS_MAP[outcome.status], outcome.detail
            )
            if outcome.status == "joined":
                result.joined.append(candidate["slug"])
                if outcome.cookies:
                    # The cookies' own domain is the page's *actual* final host
                    # -- more trustworthy than candidate["url"], which can be a
                    # discover.circle.so listing page that redirected elsewhere.
                    # This is the entire join->scrape hookup: without it, a real
                    # join never reaches scan_cookie_host()/cookie_hosts_vip_first().
                    # Not named `host`: that is this run's operator-targeting
                    # filter, and rebinding it here has no business leaking
                    # into a later iteration.
                    cookie_host = outcome.cookies[0]["domain"]
                    connect_host(db, cookie_host, outcome.cookies, member_label=candidate["name"])
                else:
                    logger.warning(
                        "joined %s but captured no session cookies -- won't be scraped "
                        "until replay_store.connect_host() is called for it manually",
                        candidate["slug"],
                    )

        sleep_between_attempts(pacing)

    return result
