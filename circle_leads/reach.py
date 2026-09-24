"""Which communities we can reach, as SQL conditions shared by every stage.

The bot, the harvest and the dashboard each used to decide this on their own,
and each decided it a little differently: the join bot queued paid communities
and directory cards the dashboard did not count, the harvest skipped
communities the dashboard counted as waiting to be read, and the dashboard
painted a queue red for paid communities that nothing will ever move.

The rule, as the user set it on 2026-09-24:

- A free community is read anonymously, or joined and then read with the
  bot's login.
- A paid community is never joined -- we do not pay -- and is read only when a
  member session for it already exists (a login the client bought). Without
  one it is only counted and listed, and takes part in nothing else.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, func, or_, select

from circle_leads.storage.models import Community, JoinStatus, ReplaySession

# Platforms we can actually join and read. "discover" is a community found
# through Circle's own directory whose platform was never narrowed -- still on
# Circle. NULL predates the platform column.
CIRCLE_PLATFORMS = ("circle", "discover")

# A directory card whose real address was never resolved points here: this is
# Circle's storefront, not the community.
DIRECTORY_HOST = "discover.circle.so"


# join_type_detail openings of an "unknown" that another look will not change:
# the host has left Circle, or never was a Circle community. Nothing re-checks
# these on a schedule, and the dashboard does not count them as waiting.
TERMINAL_UNKNOWN_PREFIXES = (
    "host no longer maps",               # redirects to circle.so's marketing site
    "HTTP 404",                          # no communities/current: not Circle
    "non-JSON response",                 # an ordinary website answered
    "custom domain no longer served",    # discovery/join_type.py::circle_host_behind
)


def recheckable_unknown():
    """An "unknown" join type that another check could still resolve."""
    detail = func.coalesce(Community.join_type_detail, "")
    return and_(
        Community.join_type == "unknown",
        *[~detail.startswith(p) for p in TERMINAL_UNKNOWN_PREFIXES],
    )


def on_circle():
    return or_(Community.platform.is_(None), Community.platform.in_(CIRCLE_PLATFORMS))


def real_host():
    """We know the community's own address."""
    return and_(Community.host.is_not(None), Community.host != DIRECTORY_HOST)


def has_session():
    """A member session (cookies) is stored for the community's host."""
    return exists(select(ReplaySession.id).where(ReplaySession.host == Community.host))


def is_paid():
    return Community.join_type == "paid"


def pursued():
    """Not a paid community we have no login for -- the only kind that is
    counted and listed but otherwise left alone."""
    return or_(Community.join_type.is_(None), ~is_paid(), has_session())


# A join-type check got one of these from the host's own Circle API: proof
# that a custom domain with no platform recorded is on Circle.
ANSWERED_AS_CIRCLE = ("free_join", "paid", "invite_only", "locked_unknown")


def proven_on_circle():
    """On Circle, and known to be: a row older than the platform column needs
    a *.circle.so host or an answer from Circle's own API."""
    return or_(
        Community.platform.in_(CIRCLE_PLATFORMS),
        and_(
            Community.platform.is_(None),
            or_(Community.host.like("%.circle.so"),
                Community.join_type.in_(ANSWERED_AS_CIRCLE)),
        ),
    )


def readable():
    """A community a reader may spend requests on: on Circle, at a known
    address, not left alone for being paid, and still alive."""
    return and_(
        proven_on_circle(),
        real_host(),
        pursued(),
        or_(Community.join_type.is_(None), Community.join_type != "subscription_expired"),
    )


# Waiting for the bot: never tried, or joined but stuck on Circle's new-member
# profile step (the membership exists; finishing it makes it readable).
JOIN_QUEUE_STATUSES = (JoinStatus.NOT_ATTEMPTED.value, JoinStatus.PROFILE_PENDING.value)


def join_queue():
    """A community the join bot should try -- and exactly what the dashboard
    counts as waiting to be joined.

    Free communities only: a paid one is never joined, since the bot does not
    pay (a visit only ever ended on a checkout page). Only with a real address:
    a directory card whose host was never resolved points at
    discover.circle.so/products/..., Circle's storefront rather than the
    community, and every such visit on 2026-09-21 ended "unclear". A host we
    already hold a member session for is read, not joined -- unless the bot
    left it on the profile step.
    """
    return and_(
        Community.icp_flag.is_(True),
        on_circle(),
        real_host(),
        Community.join_type == "free_join",
        Community.join_status.in_(JOIN_QUEUE_STATUSES),
        or_(Community.join_status == JoinStatus.PROFILE_PENDING.value, ~has_session()),
    )
