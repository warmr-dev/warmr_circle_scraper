"""Section 1: the funnel, all-time and today.

What must hold whatever the data: the parts of "fit" add up to it; today is
the viewer's day; a paid directory card without an address still counts as a
paid community; leads split by Vini's answer add up to the leads found.
"""

from datetime import timedelta

from tests.dash_fixtures import NOW, TZ_PLUS_7, community, lead, make_app, session_for

TODAY = NOW - timedelta(hours=2)            # 16:00 at UTC+7
YESTERDAY = NOW - timedelta(days=1)
EARLY_TODAY_LOCAL = NOW - timedelta(hours=17, minutes=30)  # 00:30 at UTC+7, 17:30 UTC the day before


def _seed(db):
    with db.session() as s:
        # Found yesterday, fit (checked yesterday): free, joined by the bot today.
        community(s, "free-joined", discovered_at=YESTERDAY, icp_flag=True, icp_checked_at=YESTERDAY,
                  join_type="free_join", join_status="joined", joined_at=TODAY)
        # Found today, fit today: paid, with a session stored today.
        community(s, "paid-bought", discovered_at=TODAY, icp_flag=True, icp_checked_at=TODAY,
                  join_type="paid")
        session_for(s, "paid-bought.circle.so", created_at=TODAY)
        # A paid directory card without its own address: still a paid community.
        community(s, "card", url="https://discover.circle.so/products/card", platform="discover",
                  discovered_at=YESTERDAY, icp_flag=True, icp_checked_at=YESTERDAY, join_type="paid",
                  host=None)
        # Fit, invite-only: the "other" part.
        community(s, "invite", discovered_at=YESTERDAY, icp_flag=True, icp_checked_at=YESTERDAY,
                  join_type="invite_only")
        # Fit by the flag, but out of the funnel: not on Circle; lapsed plan.
        community(s, "landing", platform="other", discovered_at=YESTERDAY, icp_flag=True,
                  icp_checked_at=YESTERDAY, join_type="free_join")
        community(s, "lapsed", discovered_at=YESTERDAY, icp_flag=True, icp_checked_at=YESTERDAY,
                  join_type="subscription_expired")
        # Not fit, found early today in the viewer's day (yesterday in UTC).
        plain = community(s, "plain", discovered_at=EARLY_TODAY_LOCAL, icp_flag=False)

        lead(s, plain, created_at=TODAY, vini_status="accepted", external_synced_at=TODAY)
        lead(s, plain, created_at=TODAY, vini_status="held",
             vini_reason="invalid_timestamp missing_or_invalid_fetched_at")
        lead(s, plain, created_at=YESTERDAY, vini_status="rejected", vini_reason="error: boom")
        lead(s, plain, created_at=YESTERDAY, external_synced_at=YESTERDAY)       # no data
        lead(s, plain, created_at=YESTERDAY)                                      # not sent
        original = lead(s, plain, created_at=YESTERDAY, vini_status="duplicate",
                        external_synced_at=YESTERDAY)
        lead(s, plain, created_at=TODAY, duplicate_of_id=original.id)            # ours: not counted
        lead(s, plain, created_at=TODAY, classification="NOT_LEAD",
             vini_status="accepted", external_synced_at=TODAY)                   # demoted: not counted


def test_the_funnel_all_time_and_today(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    data = client.get("/api/dash/analytics", params={"tz": TZ_PLUS_7}).json()
    c = data["communities"]

    assert c["found"]["all"] == 7
    # "paid-bought" found at 16:00 and "plain" at 00:30 local time.
    assert c["found"]["today"] == 2
    assert c["fit"] == {"all": 4, "today": 1}
    assert c["paid"] == {"all": 2, "today": 1, "with_session": 1}
    assert c["free"] == {"all": 1, "today": 0, "no_address": 0}
    assert c["other"] == {"all": 1, "today": 0}
    assert c["paid"]["all"] + c["free"]["all"] + c["other"]["all"] == c["fit"]["all"]
    assert c["access"]["all"] == 2 and c["access"]["today"] == 2
    assert c["access"]["fit_all"] == 2
    assert (c["access"]["joined"], c["access"]["with_session"]) == (1, 1)


def test_the_leads_split_by_what_vini_said(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    leads = client.get("/api/dash/analytics", params={"tz": TZ_PLUS_7}).json()["leads"]
    seg = leads["segments"]

    assert leads["found"] == {"all": 6, "today": 2}
    assert {k: v["all"] for k, v in seg.items()} == {
        "accepted": 1, "duplicate": 1, "held": 1, "rejected": 1, "error": 0,
        "not_sent": 1, "no_data": 1}
    assert sum(v["all"] for v in seg.values()) == leads["found"]["all"]
    assert leads["sent"] == {"all": 5, "today": 2}
    assert seg["accepted"]["today"] == 1 and seg["held"]["today"] == 1


def test_today_follows_the_viewer_not_utc(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    utc = client.get("/api/dash/analytics", params={"tz": 0}).json()["communities"]
    # At UTC, "plain" (17:30 UTC yesterday) is not today.
    assert utc["found"]["today"] == 1


def test_an_empty_database_is_all_zeros(tmp_path, monkeypatch):
    client, _db, _app = make_app(tmp_path, monkeypatch)
    data = client.get("/api/dash/analytics").json()
    assert data["communities"]["found"] == {"all": 0, "today": 0, "circle_all": 0, "circle_today": 0}
    assert data["leads"]["found"] == {"all": 0, "today": 0}
    assert data["today_start"].endswith("Z")


def test_reading_counts_every_feed_that_answers_and_every_working_session(tmp_path, monkeypatch):
    """The user, 2026-09-29: "why 57 inside, but 85 read every 2 minutes and
    101 every 15?" -- an open community is read without joining it. The page
    now counts what we read, anonymously or with a session, as its own number.
    """
    from tests.dash_fixtures import connection, watch

    client, db, _app = make_app(tmp_path, monkeypatch)
    with db.session() as s:
        # Open, read anonymously every 2 minutes, a new post today.
        open_fast = community(s, "open-fast", host="open-fast.circle.so", icp_flag=True)
        watch(s, open_fast, last_status="ok", last_new_at=TODAY)
        # Open and quiet: every 15 minutes, 304s.
        quiet = community(s, "quiet", host="quiet.circle.so")
        watch(s, quiet, tier="slow", last_status="not_modified", last_new_at=YESTERDAY)
        # A member: the feed is read anonymously, the session reads the rest.
        member = community(s, "member", host="member.circle.so", icp_flag=True,
                           join_status="joined")
        watch(s, member, last_status="ok")
        session_for(s, "member.circle.so")
        connection(s, "member.circle.so", "connected")
        # Private, no session: the feed refuses us -- not read.
        private = community(s, "private", host="private.circle.so", icp_flag=True)
        watch(s, private, mode="off", last_status="unauthorized")
        # A session that died, and its feed refuses anonymous reads: not read.
        dead = community(s, "dead", host="dead.circle.so", join_status="joined")
        watch(s, dead, mode="cookie", last_status="unauthorized")
        session_for(s, "dead.circle.so")
        connection(s, "dead.circle.so", "session_expired")
        # Read only through its session: the feed is off, the scan works.
        scan_only = community(s, "scan-only", host="scan-only.circle.so")
        session_for(s, "scan-only.circle.so")
        connection(s, "scan-only.circle.so", "connected")

    r = client.get("/api/dash/analytics", params={"tz": TZ_PLUS_7}).json()["communities"]["reading"]
    assert r["all"] == 4                      # open-fast, quiet, member, scan-only
    assert r["with_session"] == 2             # member, scan-only
    assert r["anonymous"] == 2
    assert r["with_session"] + r["anonymous"] == r["all"]
    assert r["fit"] == 2                      # open-fast, member
    assert r["feed_fast"] == 2 and r["feed_slow"] == 1
    assert r["today"] == 1                    # open-fast brought a post today
