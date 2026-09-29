"""The helpers the dashboard sections share: the work queues and the cookie
connection buckets (circle_leads/web/overview.py).

The client-facing overview built on them was replaced by the dashboard's
sections; its funnel invariants live in tests/test_dash_analytics.py. What
stays here is who waits in which queue -- the reading rules of
circle_leads/reach.py, as the attention list reports them.
"""

from datetime import timedelta

from sqlalchemy import and_

from circle_leads.storage.database import Database, get_or_create_community, utcnow
from circle_leads.storage.models import Community, JoinStatus, ReplaySession
from circle_leads.web.overview import (
    DEAD_HOST_MARKER, DNS_SOURCE, build_queues, connection_bucket,
)


def _row(s, slug, *, source, name=None, platform="circle", join_type=None,
         icp=False, join_status=JoinStatus.NOT_ATTEMPTED.value, read=False):
    c = get_or_create_community(s, slug=slug, url=f"https://{slug}.circle.so")
    c.discovery_source, c.name, c.platform, c.join_type = source, name, platform, join_type
    c.icp_flag, c.join_status = icp, join_status
    c.last_synced_at = utcnow() if read else None
    return c


def _session(s, host):
    s.add(ReplaySession(host=host, encrypted_cookies="x", cookie_count=1))


def _queues(db, now=None):
    now = now or utcnow().replace(tzinfo=None)
    named = and_(Community.name.is_not(None), Community.name != "")
    with db.session() as s:
        return {q["key"]: q for q in build_queues(s, now, named=named)}


def test_connection_bucket():
    assert connection_bucket("connected", None) == "working"
    assert connection_bucket("error", "Unexpected Cloudflare challenge on x") == "cloudflare_blocked"
    assert connection_bucket("error", "boom") == "error"
    assert connection_bucket("session_expired", None) == "session_expired"
    assert connection_bucket("not_connected", None) == "not_connected"


def test_each_queue_counts_what_its_worker_will_take(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'q.db'}")
    now = utcnow().replace(tzinfo=None)  # naive UTC, like the database
    with db.session() as s:
        _row(s, "live", source=DNS_SOURCE, name="L", join_type="free_join", icp=True)
        joined = _row(s, "joined", source="circle_directory", name="J",
                      join_type="free_join", icp=True, join_status=JoinStatus.JOINED.value)
        joined.join_attempted_at = now - timedelta(days=3)
        joined.last_synced_at = now - timedelta(hours=1)
        unchecked = _row(s, "unchecked", source=DNS_SOURCE, name="U", join_type="unknown")
        unchecked.join_type_checked_at = now - timedelta(hours=2)
        _row(s, "lapsed", source=DNS_SOURCE, name="X", join_type="subscription_expired")
        _row(s, "untyped", source="builtwith_lists", name="N")
        _row(s, "unnamed", source=DNS_SOURCE)
        dead = _row(s, "dead", source=DNS_SOURCE)
        dead.icp_reasons = ["no_metadata", DEAD_HOST_MARKER]
        _row(s, "paid", source="circle_directory", name="P", platform="discover",
             join_type="paid", icp=True)
        unread = _row(s, "unread", source=DNS_SOURCE, name="R", join_type="locked_unknown",
                      icp=True)
        # Asked yesterday: its weekly join-type look is not due yet.
        unread.join_type_checked_at = now - timedelta(days=1)

    q = _queues(db, now)
    assert list(q) == ["name", "join_type_recheck", "read", "join"]
    assert q["name"]["waiting"] == 1          # the dead host is not waiting for a name
    assert not q["name"]["stale"]
    assert q["join_type_recheck"]["waiting"] == 1
    assert q["join_type_recheck"]["field"] == "join_type_checked_at"
    assert not q["join_type_recheck"]["stale"]
    # The read queue counts what a reader will take: the free and the locked
    # community. The paid one has no login, so nothing reads it -- by decision.
    assert q["read"]["waiting"] == 2 and not q["read"]["stale"]
    # One free community waits and the last join attempt was 3 days ago.
    assert q["join"]["waiting"] == 1 and q["join"]["stale"]


def test_empty_queue_is_never_stale(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'e.db'}")
    assert all(x["waiting"] == 0 and not x["stale"] for x in _queues(db).values())


def test_a_community_that_is_not_on_circle_is_in_no_queue(tmp_path):
    """The join queue once said 87 for 21 joinable communities: 66 were
    marketing sites that merely had a directory card."""
    db = Database(f"sqlite:///{tmp_path / 'landing.db'}")
    with db.session() as s:
        _row(s, "landing", source="circle_directory", name="C", platform="other",
             join_type="free_join", icp=True)
        _row(s, "real", source="circle_directory", name="R", join_type="free_join", icp=True)
    q = _queues(db)
    assert q["join"]["waiting"] == 1
    assert q["read"]["waiting"] == 1


def test_a_community_whose_address_we_do_not_know_is_not_waiting_to_be_read(tmp_path):
    """276 of prod's ICP-fit rows are directory cards with no host resolved."""
    db = Database(f"sqlite:///{tmp_path / 'nohost.db'}")
    with db.session() as s:
        carded = _row(s, "card", source="circle_directory", name="C",
                      platform="discover", join_type="paid", icp=True)
        carded.host = None
        carded.url = "https://discover.circle.so/products/card"
        reachable = _row(s, "reach", source="circle_directory", name="R",
                         join_type="free_join", icp=True)
        reachable.host = "reach.circle.so"
    assert _queues(db)["read"]["waiting"] == 1


def test_a_paid_community_with_a_login_is_read(tmp_path):
    """Paid is read only with a login someone bought -- and then it counts."""
    db = Database(f"sqlite:///{tmp_path / 'paid.db'}")
    with db.session() as s:
        _row(s, "bought", source="circle_directory", name="Bought", join_type="paid", icp=True)
        _row(s, "not-bought", source="circle_directory", name="Other", join_type="paid", icp=True)
        _session(s, "bought.circle.so")
    q = _queues(db)
    assert q["read"]["waiting"] == 1
    assert q["join"]["waiting"] == 0  # paid is never joined


def test_an_unknown_that_left_circle_is_not_waiting_for_a_recheck(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'gone.db'}")
    with db.session() as s:
        for slug, detail in [
            ("gone", "host no longer maps to a community (redirects to circle.so marketing site)"),
            ("site", "non-JSON response"),
            ("missing", "HTTP 404"),
            ("moved-away", "custom domain no longer served: TLS fails on x and y"),
            ("tls", "request failed: SSLError"),
            ("legacy", None),
        ]:
            r = _row(s, slug, source=DNS_SOURCE, name=slug, join_type="unknown")
            r.join_type_detail = detail
    # Only the TLS failure (its CNAME is followed next time) and the row
    # checked before reasons were recorded can still change.
    assert _queues(db)["join_type_recheck"]["waiting"] == 2


def test_a_free_community_we_hold_a_session_for_is_joined_not_waiting(tmp_path):
    """siliconslopes sat in the join queue while being read with its session."""
    db = Database(f"sqlite:///{tmp_path / 'sess.db'}")
    with db.session() as s:
        _row(s, "read-by-cookie", source=DNS_SOURCE, name="S", join_type="free_join", icp=True)
        _row(s, "to-join", source=DNS_SOURCE, name="T", join_type="free_join", icp=True)
        _session(s, "read-by-cookie.circle.so")
    assert _queues(db)["join"]["waiting"] == 1


def test_a_closed_fit_community_waits_for_its_weekly_look(tmp_path):
    """The scheduled join-type pass asks closed ICP-fit communities again once
    a week (reach.closed_recheck_due); the queue counts the ones that are due."""
    db = Database(f"sqlite:///{tmp_path / 'closed.db'}")
    now = utcnow().replace(tzinfo=None)
    with db.session() as s:
        due = _row(s, "due", source=DNS_SOURCE, name="D", join_type="invite_only", icp=True)
        due.join_type_checked_at = now - timedelta(days=8)
        fresh = _row(s, "fresh", source=DNS_SOURCE, name="F", join_type="invite_only", icp=True)
        fresh.join_type_checked_at = now - timedelta(days=2)
        not_fit = _row(s, "not-fit", source=DNS_SOURCE, name="N", join_type="locked_unknown")
        not_fit.join_type_checked_at = now - timedelta(days=30)
    assert _queues(db, now)["join_type_recheck"]["waiting"] == 1
