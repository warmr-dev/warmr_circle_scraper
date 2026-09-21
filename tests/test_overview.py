"""The client-facing overview: funnel by source, ICP groups, cookie states."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from circle_leads.storage.database import Database, get_or_create_community, utcnow  # noqa: E402
from circle_leads.storage.models import CircleConnection, JoinStatus  # noqa: E402
from circle_leads.web.overview import DNS_SOURCE, connection_bucket  # noqa: E402

PASSWORD = "dashboard-test-pw"
_DEV_CONFIG = str(Path(__file__).parent / "fixtures" / "dev_requirements.yaml")


def _row(s, slug, *, source, name=None, platform="circle", join_type=None,
         icp=False, join_status=JoinStatus.NOT_ATTEMPTED.value, read=False):
    c = get_or_create_community(s, slug=slug, url=f"https://{slug}.circle.so")
    c.discovery_source, c.name, c.platform, c.join_type = source, name, platform, join_type
    c.icp_flag, c.join_status = icp, join_status
    c.last_synced_at = utcnow() if read else None
    return c


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-signing-key")
    url = f"sqlite:///{tmp_path / 'ov.db'}"
    db = Database(url)
    with db.session() as s:
        # Directory: free + joined + read, a paid directory card, a non-Circle
        # landing page (ICP-fit but excluded).
        _row(s, "dir-free", source="circle_directory", name="A", join_type="free_join",
             icp=True, join_status=JoinStatus.JOINED.value, read=True)
        _row(s, "dir-card", source="circle_directory", name="B", platform="discover",
             join_type="paid", icp=True)
        _row(s, "dir-landing", source="circle_directory", name="C", platform="other",
             join_type="free_join", icp=True)
        # DNS: an unnamed 401 (not alive), a named locked one (ICP), a lapsed
        # subscription (ICP-fit but excluded), an unnamed free_join (alive).
        _row(s, "dns-401", source=DNS_SOURCE, join_type="locked_unknown")
        _row(s, "dns-locked", source=DNS_SOURCE, name="D", join_type="locked_unknown", icp=True)
        _row(s, "dns-lapsed", source=DNS_SOURCE, name="E", join_type="subscription_expired",
             icp=True, read=True)
        _row(s, "dns-free", source=DNS_SOURCE, join_type="free_join")
        # Other: paid on Circle that the bot stopped at checkout.
        _row(s, "other-paywall", source="builtwith_lists", name="F", join_type="free_join",
             icp=True, join_status=JoinStatus.PAID_SKIP.value)
        s.add_all([
            CircleConnection(host="ok.circle.so", state="connected"),
            CircleConnection(host="cf.circle.so", state="error",
                             state_detail="Unexpected Cloudflare challenge on /internal_api/spaces."),
            CircleConnection(host="old.circle.so", state="session_expired",
                             state_detail="Session cookie invalid or expired -- refresh it."),
        ])
    cfg = tmp_path / "requirements.yaml"
    shutil.copy(_DEV_CONFIG, cfg)
    c = TestClient(__import__("circle_leads.web.app", fromlist=["create_app"])
                   .create_app(db_url=url, config_path=str(cfg)))
    c.post("/login", data={"password": PASSWORD})
    return c


def test_overview_requires_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    from circle_leads.web.app import create_app
    anon = TestClient(create_app(db_url=f"sqlite:///{tmp_path / 'a.db'}"))
    assert anon.get("/api/overview").status_code == 401


def test_funnel_splits_by_source_and_ignores_a_bare_401(client):
    f = client.get("/api/overview").json()["funnel"]
    by = {x["key"]: x for x in f["sources"]}
    assert by["directory"]["found"] == 3 and by["directory"]["alive"] == 3
    # dns-401 is not alive (Circle 401s invented subdomains); dns-free is,
    # without a name.
    assert by["dns"]["found"] == 4
    assert by["dns"]["alive"] == 3
    assert by["dns"]["named"] == 2
    # dns-lapsed is ICP-fit but excluded; dns-locked counts.
    assert by["dns"]["icp"] == 1
    assert by["directory"]["icp"] == 2  # landing page excluded
    assert by["directory"]["read"] == 1
    # The lapsed community was read, but it is not in the ICP count.
    assert by["dns"]["read"] == 0 and by["dns"]["read_other"] == 1
    assert f["total"]["found"] == 8
    assert f["updated"]["found"]


def test_icp_groups_by_access_and_exclusions(client):
    g = client.get("/api/overview").json()["icp_groups"]
    assert g["free"] == {"joined": 1, "waiting": 0, "hit_paywall": 1, "failed": 0, "total": 2}
    assert g["paid"]["directory_cards"] == 1 and g["paid"]["total"] == 1
    assert g["closed"]["locked"] == 1 and g["closed"]["total"] == 1
    assert g["excluded"] == {"not_on_circle": 1, "subscription_expired": 1, "total": 2}
    assert g["total"] == 4


def test_cloudflare_block_is_not_reported_as_an_expired_cookie(client):
    conn = client.get("/api/overview").json()["connections"]
    assert conn["counts"] == {"working": 1, "cloudflare_blocked": 1, "session_expired": 1}
    refresh = client.get("/api/connections/to-refresh").json()["to_refresh"]
    assert [r["host"] for r in refresh] == ["old.circle.so"]


def test_connection_bucket():
    assert connection_bucket("connected", None) == "working"
    assert connection_bucket("error", "Unexpected Cloudflare challenge on x") == "cloudflare_blocked"
    assert connection_bucket("error", "boom") == "error"
    assert connection_bucket("session_expired", None) == "session_expired"
    assert connection_bucket("not_connected", None) == "not_connected"


def test_named_split_and_queues(tmp_path):
    from datetime import timedelta

    from circle_leads.web.overview import DEAD_HOST_MARKER, build_overview

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
        _row(s, "unread", source=DNS_SOURCE, name="R", join_type="locked_unknown", icp=True)

    with db.session() as s:
        ov = build_overview(s, now)

    t = ov["funnel"]["total"]
    assert t["named"] == 7 and t["live"] == 4
    # Every named row lands in exactly one bucket.
    assert (t["unchecked"], t["named_lapsed"], t["named_untyped"]) == (1, 1, 1)
    assert t["live"] + t["unchecked"] + t["named_lapsed"] + t["named_untyped"] == t["named"]

    q = {x["key"]: x for x in ov["queues"]}
    assert q["name"]["waiting"] == 1          # the dead host is not waiting for a name
    assert not q["name"]["stale"]
    assert q["join_type_recheck"]["waiting"] == 1
    assert q["join_type_recheck"]["field"] == "join_type_checked_at"
    assert not q["join_type_recheck"]["stale"]
    assert q["read"]["waiting"] == 2 and not q["read"]["stale"]
    # One free community waits and the last join attempt was 3 days ago.
    assert q["join"]["waiting"] == 1 and q["join"]["stale"]
    # A paid community waits and no decision was ever recorded.
    assert q["paid_decision"]["waiting"] == 1
    assert q["paid_decision"]["last_moved"] is None and q["paid_decision"]["stale"]


def test_empty_queue_is_never_stale(tmp_path):
    from circle_leads.web.overview import build_overview

    db = Database(f"sqlite:///{tmp_path / 'e.db'}")
    with db.session() as s:
        queues = build_overview(s, utcnow().replace(tzinfo=None))["queues"]
    assert all(x["waiting"] == 0 and not x["stale"] for x in queues)


def test_overview_is_served_from_a_short_cache(client, monkeypatch):
    import circle_leads.web.app as web_app
    import circle_leads.web.overview as overview

    real = overview.build_overview
    calls = []

    def counting(s, now):
        calls.append(now)
        return real(s, now)

    monkeypatch.setattr(overview, "build_overview", counting)
    first = client.get("/api/overview").json()
    second = client.get("/api/overview").json()
    assert second == first
    assert len(calls) == 1

    monkeypatch.setattr(web_app, "OVERVIEW_TTL_SECONDS", 0)
    client.get("/api/overview")
    assert len(calls) == 2
