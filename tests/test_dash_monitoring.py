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


def test_the_summary_counts_what_is_polled(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    s = client.get("/api/dash/monitoring", params={"tz": TZ_PLUS_7}).json()["summary"]
    assert (s["total"], s["active"], s["off"]) == (3, 2, 1)
    assert (s["fast"], s["slow"], s["with_session"]) == (1, 1, 1)
    assert s["failing"] == 1
    assert (s["leads_today"], s["communities_with_leads_today"]) == (1, 1)


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
