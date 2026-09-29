"""Section 2: the communities the watcher checks, and their leads.

Leads come from the leads table, not from the watcher's own counter: the
harvest and the cookie scans file leads for the same communities.
"""

from datetime import timedelta

from tests.dash_fixtures import (
    NOW, TZ_PLUS_7, community, connection, lead, make_app, session_for, watch,
)


def _seed(db):
    with db.session() as s:
        busy = community(s, "busy", name="Busy", icp_flag=True, join_status="joined")
        watch(s, busy, mode="cookie", last_status="ok", last_checked_at=NOW, leads_found=0)
        session_for(s, "busy.circle.so")
        connection(s, "busy.circle.so", "connected")
        lead(s, busy, created_at=NOW - timedelta(hours=1))              # today
        lead(s, busy, created_at=NOW - timedelta(days=3))               # earlier
        lead(s, busy, created_at=NOW, classification="NOT_LEAD")        # not a lead
        first = lead(s, busy, created_at=NOW - timedelta(days=4))
        lead(s, busy, created_at=NOW, duplicate_of_id=first.id)         # our duplicate

        broken = community(s, "broken", name="Broken")
        watch(s, broken, mode="anon", tier="slow", last_status="error", consecutive_errors=6)

        closed = community(s, "closed", name="Closed")
        watch(s, closed, mode="off", last_status="unauthorized")

        # A session with no watch row at all.
        session_for(s, "orphan.circle.so", plaintext=True)
        connection(s, "orphan.circle.so", "session_expired", "Session cookie invalid")


def test_rows_carry_leads_from_the_leads_table(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    data = client.get("/api/dash/monitoring", params={"tz": TZ_PLUS_7}).json()
    rows = {r["slug"]: r for r in data["rows"]}

    busy = rows["busy"]
    assert (busy["leads_all"], busy["leads_today"]) == (3, 1)
    assert busy["member"] and busy["has_session"]
    assert busy["conn"]["bucket"] == "working"
    assert busy["session"]["cookie_count"] == 2
    assert rows["broken"]["leads_all"] == 0
    # Most leads first, switched-off rows last.
    assert [r["slug"] for r in data["rows"]] == ["busy", "broken", "closed"]


def test_the_summary_counts_what_is_read(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    s = client.get("/api/dash/monitoring", params={"tz": TZ_PLUS_7}).json()["summary"]
    assert s["total"] == 3
    assert (s["reading"], s["broken"], s["private"], s["other"]) == (1, 0, 1, 1)
    assert (s["feed_fast"], s["feed_slow"], s["session_ok"]) == (1, 0, 1)
    assert (s["leads_today"], s["communities_with_leads_today"]) == (1, 1)


def test_each_row_says_whether_it_is_read_was_read_or_never_was(tmp_path, monkeypatch):
    """The user, 2026-09-29: monitoring shows what we read -- what works or
    worked -- and a feed that refuses a visitor is private, not "no access"."""
    client, db, _app = make_app(tmp_path, monkeypatch)
    with db.session() as s:
        # Read before: posts on file, and now the feed errors.
        was = community(s, "was-read", host="was-read.circle.so")
        watch(s, was, last_status="error", consecutive_errors=3)
        lead(s, was, created_at=NOW - timedelta(days=5))
        # The feed is off, but the session reads it: still read.
        scanned = community(s, "scanned", host="scanned.circle.so", join_status="joined")
        watch(s, scanned, mode="off", last_status="unauthorized")
        session_for(s, "scanned.circle.so")
        connection(s, "scanned.circle.so", "connected")
        # Never read, refused: private.
        private = community(s, "private", host="private.circle.so")
        watch(s, private, mode="off", last_status="unauthorized")
        # Never read, moved away.
        moved = community(s, "moved", host="moved.circle.so")
        watch(s, moved, tier="slow", last_status="http_301")
    rows = {r["slug"]: r for r in client.get("/api/dash/monitoring").json()["rows"]}
    assert rows["was-read"]["group"] == "broken"
    assert rows["scanned"]["group"] == "ok"
    assert rows["scanned"]["session_ok"] and not rows["scanned"]["feed_ok"]
    assert rows["private"]["group"] == "private"
    assert rows["moved"]["group"] == "other"


def test_the_account_is_the_joins_or_else_a_session_label_naming_one(tmp_path, monkeypatch):
    from circle_leads.storage.models import ReplaySession
    from sqlalchemy import select

    client, db, _app = make_app(tmp_path, monkeypatch)
    with db.session() as s:
        labelled = community(s, "labelled", host="labelled.circle.so", join_account="main")
        watch(s, labelled, last_status="ok")
        session_for(s, "labelled.circle.so")
        s.scalar(select(ReplaySession).where(
            ReplaySession.host == "labelled.circle.so")).member_label = "test"
        joined = community(s, "joined", host="joined.circle.so", join_status="joined",
                           join_account="4")
        watch(s, joined, last_status="ok")
        nobody = community(s, "nobody", host="nobody.circle.so")
        watch(s, nobody, last_status="ok")
        # An old session label that is the community's name, not an account.
        legacy = community(s, "legacy", host="legacy.circle.so", join_status="joined")
        watch(s, legacy, last_status="ok")
        session_for(s, "legacy.circle.so")
        s.scalar(select(ReplaySession).where(
            ReplaySession.host == "legacy.circle.so")).member_label = "Speak_ Roblox"
        only_label = community(s, "only-label", host="only-label.circle.so")
        watch(s, only_label, last_status="ok")
        session_for(s, "only-label.circle.so")
        s.scalar(select(ReplaySession).where(
            ReplaySession.host == "only-label.circle.so")).member_label = "client (paid)"
    rows = {r["slug"]: r for r in client.get("/api/dash/monitoring").json()["rows"]}
    # The recorded join account wins over whatever the session says.
    assert rows["labelled"]["account"] == "main"
    assert rows["joined"]["account"] == "4"
    assert rows["nobody"]["account"] is None
    assert rows["legacy"]["account"] is None
    assert rows["only-label"]["account"] == "client (paid)"


def test_a_session_without_a_watch_row_is_listed_apart(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    data = client.get("/api/dash/monitoring").json()
    orphans = {u["host"]: u for u in data["unwatched_connections"]}
    assert set(orphans) == {"orphan.circle.so"}
    assert orphans["orphan.circle.so"]["session"]["plaintext"] is True


def test_no_cookie_value_ever_leaves(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    body = client.get("/api/dash/monitoring").text
    assert "enc:x" not in body and "plain:x" not in body
    assert "encrypted_cookies" not in body
