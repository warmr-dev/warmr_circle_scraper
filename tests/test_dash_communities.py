"""Section 4: the whole communities table -- server-side search, filters,
paging -- and the panel that opens on one community."""

from datetime import timedelta

from circle_leads.storage.activity import log_activity
from tests.dash_fixtures import NOW, community, lead, make_app, session_for, watch


def _seed(db, n=120):
    with db.session() as s:
        for i in range(n):
            community(s, f"c{i:03d}", name=f"Community {i:03d}", icp_score=i / 10,
                      discovered_at=NOW - timedelta(days=i),
                      discovery_source="circle_directory" if i % 2 else "cli")
        fit = community(s, "fit-free", name="Fit Free", icp_flag=True, join_type="free_join",
                        icp_score=99.5, read_outcome="public")
        watch(s, fit, mode="anon")
        community(s, "off-circle", name="Off Circle", platform="other", icp_flag=True,
                  join_type="free_join")
        joined = community(s, "joined", name="Joined", icp_flag=True, join_type="free_join",
                           join_status="joined")
        session_for(s, "joined.circle.so")
        lead(s, joined)
        lead(s, joined)


def _get(client, **params):
    r = client.get("/api/dash/communities", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_paging_covers_every_row_once(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    seen = []
    for page in (1, 2, 3):
        data = _get(client, page=page, limit=50, sort="name", dir="asc")
        assert data["total"] == 123
        seen += [r["slug"] for r in data["rows"]]
    assert len(seen) == len(set(seen)) == 123


def test_filters(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    assert {r["slug"] for r in _get(client, icp="fit")["rows"]} == {"fit-free", "joined"}
    assert {r["slug"] for r in _get(client, icp="flag")["rows"]} == {"fit-free", "joined", "off-circle"}
    assert [r["slug"] for r in _get(client, platform="other")["rows"]] == ["off-circle"]
    assert [r["slug"] for r in _get(client, monitored="1")["rows"]] == ["fit-free"]
    assert [r["slug"] for r in _get(client, access="1")["rows"]] == ["joined"]
    assert [r["slug"] for r in _get(client, read_outcome="public")["rows"]] == ["fit-free"]
    assert _get(client, read_outcome="__null__")["total"] == 122
    assert _get(client, source="directory")["total"] == 60
    assert [r["slug"] for r in _get(client, q="fit fr")["rows"]] == ["fit-free"]


def test_sorting_by_leads_and_by_icp(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    top = _get(client, sort="leads")["rows"][0]
    assert (top["slug"], top["leads"]) == ("joined", 2)
    assert _get(client, sort="icp")["rows"][0]["slug"] == "fit-free"
    assert client.get("/api/dash/communities", params={"sort": "drop table"}).status_code == 400


def test_facets_count_every_value(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    facets = client.get("/api/dash/communities/facets").json()
    assert {f["value"]: f["count"] for f in facets["platform"]} == {"circle": 122, "other": 1}
    assert sum(f["count"] for f in facets["join_type"]) == 123


def test_the_panel_has_leads_and_activity_under_any_name(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    with db.session() as s:
        # Writers name a community by slug, by host, or by the host's first label.
        log_activity(s, kind="read", community="joined", summary="by slug")
        log_activity(s, kind="ingest", community="joined.circle.so", summary="by host")
        log_activity(s, kind="review", community="unrelated", summary="someone else")
    cid = _get(client, access="1")["rows"][0]["id"]
    panel = client.get(f"/api/dash/communities/{cid}").json()
    assert panel["community"]["slug"] == "joined"
    assert panel["fit"] is True
    assert len(panel["leads"]) == 2
    assert {a["summary"] for a in panel["activity"]} == {"by slug", "by host"}
    assert panel["session"]["cookie_count"] == 2
    assert "encrypted_cookies" not in str(panel)
    assert client.get("/api/dash/communities/999999").status_code == 404
