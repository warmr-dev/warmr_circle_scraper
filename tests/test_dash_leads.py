"""Section 3: every lead, split by what Vini said, with the reason.

The segments add up to the whole; each not-sent lead says why in our words;
Vini's reasons are offered as filters; the page never loses a lead to a
segment it does not show.
"""

from datetime import timedelta

from tests.dash_fixtures import NOW, TZ_PLUS_7, community, lead, make_app

HOLD = "invalid_timestamp missing_or_invalid_fetched_at"


def _seed(db):
    with db.session() as s:
        acme = community(s, "acme", name="Acme")
        other = community(s, "other", name="Other Place")
        lead(s, acme, vini_status="accepted", external_synced_at=NOW, vini_reason="inserted",
             job_title="Flutter developer")
        lead(s, acme, vini_status="duplicate", external_synced_at=NOW, vini_reason="duplicate")
        lead(s, acme, vini_status="held", vini_reason=HOLD)
        lead(s, acme, vini_status="held", vini_reason=HOLD, created_at=NOW - timedelta(days=2))
        lead(s, acme, vini_status="rejected", vini_reason="error: content is required")
        lead(s, acme, vini_status="error", vini_reason="Vini ingest HTTP 503: down")
        lead(s, other, external_synced_at=NOW - timedelta(days=10),
             created_at=NOW - timedelta(days=10))                                  # no data
        lead(s, other, created_at=NOW - timedelta(days=1))                          # pending
        lead(s, other, author=False)                                                # no author
        lead(s, other, reason="Hiring intent. Held for review, not sent to Vini: no LLM verdict")
        first = lead(s, other)
        lead(s, other, duplicate_of_id=first.id)                                    # our copy
        lead(s, other, classification="NOT_LEAD", content="Hi all, I offer SEO services.")


def _get(client, **params):
    r = client.get("/api/dash/leads", params={"tz": TZ_PLUS_7, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_segments_add_up_to_every_lead(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    counts = _get(client)["counts"]
    assert counts == {"accepted": 1, "duplicate": 1, "held": 2, "rejected": 1, "error": 1,
                      "not_sent": 6, "no_data": 1, "all": 13}


def test_not_sent_says_why_in_our_words(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    data = _get(client, segment="not_sent")
    assert {r["reason"]: r["count"] for r in data["reasons"]["not_sent"]} == {
        "pending": 2, "no_author": 1, "held_for_review": 1, "duplicate_of": 1, "not_lead": 1}
    assert all(r["segment"] == "not_sent" and r["not_sent_reason"] for r in data["rows"])
    only = _get(client, segment="not_sent", reason="no_author")
    assert only["filtered"] == 1 and len(only["rows"]) == 1
    # The tabs and the other reasons stay, so the reader can move to them.
    assert only["counts"]["not_sent"] == 6
    assert len(only["reasons"]["not_sent"]) == 5


def test_vinis_reasons_are_filters(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    data = _get(client, segment="held,rejected,error")
    assert data["filtered"] == 4
    chips = {(r["segment"], r["reason"]): r["count"] for r in data["reasons"]["vini"]}
    assert chips[("held", HOLD)] == 2
    held = _get(client, segment="held,rejected,error", reason=HOLD)
    assert [r["vini_status"] for r in held["rows"]] == ["held", "held"]


def test_search_period_and_paging(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    assert [r["job_title"] for r in _get(client, q="flutter")["rows"]] == ["Flutter developer"]
    assert _get(client, q="Other Place")["counts"]["all"] == 7
    # "since today" at UTC+7: leads created at NOW; the older ones drop out.
    today = _get(client, since="2026-09-29")
    assert today["counts"]["all"] == 10
    page1 = _get(client, limit=5, page=1)
    page3 = _get(client, limit=5, page=3)
    assert len(page1["rows"]) == 5 and len(page3["rows"]) == 3
    ids = {r["id"] for r in page1["rows"]} | {r["id"] for r in _get(client, limit=5, page=2)["rows"]}
    assert not ids & {r["id"] for r in page3["rows"]}


def test_bad_input_is_a_400(tmp_path, monkeypatch):
    client, _db, _app = make_app(tmp_path, monkeypatch)
    assert client.get("/api/dash/leads", params={"segment": "nope"}).status_code == 400
    assert client.get("/api/dash/leads", params={"sort": "nope"}).status_code == 400
    assert client.get("/api/dash/leads", params={"since": "29.09"}).status_code == 400


def test_one_lead_in_full(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    _seed(db)
    held = _get(client, segment="held")["rows"][0]
    detail = client.get(f"/api/dash/leads/{held['id']}").json()
    assert detail["vini_status"] == "held" and detail["vini_reason"] == HOLD
    assert detail["content"]
    assert client.get("/api/dash/leads/999999").status_code == 404


def test_a_row_carries_what_the_lead_card_shows(tmp_path, monkeypatch):
    """The list is the first dashboard's lead cards again (2026-09-30): the
    whole post, clamped on the page, and what the model pulled out of it."""
    client, db, _app = make_app(tmp_path, monkeypatch)
    long_post = "We need a Flutter developer. " * 30
    with db.session() as s:
        acme = community(s, "acme", name="Acme")
        lead(s, acme, content=long_post, skills=["Flutter", "Dart"], employment_type="Contract",
             hire_target="agency", budget="$100/hr", location="Remote", urgency="High",
             decided_by="llm", reason="Clear hiring ask")
    (row,) = _get(client)["rows"]
    assert row["content"] == long_post.strip()
    assert row["snippet"].endswith("…")
    assert row["skills"] == ["Flutter", "Dart"]
    assert (row["employment_type"], row["hire_target"], row["budget"], row["location"],
            row["urgency"]) == ("Contract", "agency", "$100/hr", "Remote", "High")
    assert row["decided_by"] == "llm" and row["reason"] == "Clear hiring ask"
    assert row["confidence"] == 0.9
    assert row["community"]["url"] == "https://acme.circle.so"
