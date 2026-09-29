"""Section 5, the log: filters, and paging by id that new rows cannot shift."""

from datetime import timedelta

from circle_leads.storage.models import ActivityLog
from tests.dash_fixtures import NOW, make_app


def _seed(db, n=250):
    with db.session() as s:
        for i in range(n):
            s.add(ActivityLog(
                kind="join" if i % 5 == 0 else "read",
                level="warning" if i % 10 == 0 else "info",
                community=f"c{i % 3}", summary=f"row {i}", detail={"i": i},
                created_at=NOW - timedelta(minutes=n - i), items_seen=0, leads_found=0))


def test_paging_by_id_walks_every_row_once(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    seen, before = [], None
    for _ in range(10):
        params = {"limit": 100, **({"before_id": before} if before else {})}
        page = client.get("/api/dash/activity", params=params).json()
        seen += [r["id"] for r in page["rows"]]
        before = page["next_before_id"]
        if before is None:
            break
    assert len(seen) == len(set(seen)) == 250
    assert seen == sorted(seen, reverse=True)


def test_filters(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    joins = client.get("/api/dash/activity", params={"kind": "join", "limit": 200}).json()["rows"]
    assert len(joins) == 50 and {r["kind"] for r in joins} == {"join"}
    problems = client.get("/api/dash/activity", params={"level": "warning,error", "limit": 200}).json()
    assert len(problems["rows"]) == 25
    one = client.get("/api/dash/activity", params={"q": "row 7", "limit": 200}).json()["rows"]
    assert {r["summary"] for r in one} == {"row 7", "row 70", "row 71", "row 72", "row 73", "row 74",
                                           "row 75", "row 76", "row 77", "row 78", "row 79"}
    last_hour = client.get("/api/dash/activity", params={"hours": 1, "limit": 200}).json()["rows"]
    assert len(last_hour) == 60
