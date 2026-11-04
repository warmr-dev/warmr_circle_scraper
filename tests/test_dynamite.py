"""Dynamite Circle reader: Firestore mapping, session at rest, no live network."""

import json
from datetime import datetime, timezone

from circle_leads.config.settings import Requirements
from circle_leads.scraper import dynamite
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community
from circle_leads.storage.settings_store import get_setting, set_setting
from circle_leads.triage.pipeline import TriageResult


def _db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/dynamite.db")


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _room(**overrides):
    fields = {
        "name": {"stringValue": "Need a React engineer"},
        "body": {"stringValue": "<p>We are <b>hiring</b> a developer. Write a@b.co</p>"},
        "createdAt": {"timestampValue": "2026-11-03T12:00:00Z"},
        "authorID": {"stringValue": "author-1"},
        "authorUser": {"mapValue": {"fields": {
            "displayName": {"stringValue": "Ada"},
            "userID": {"stringValue": "user-1"},
            "userName": {"stringValue": "ada"},
        }}},
        "flags": {"mapValue": {"fields": {
            "isDeleted": {"booleanValue": False},
            "isSunk": {"booleanValue": False},
            "isHidden": {"booleanValue": False},
        }}},
    }
    fields.update(overrides)
    return {
        "name": "projects/dc-nav/databases/(default)/documents/rooms/room1234567890123456",
        "fields": fields,
    }


def _message(message_id, *, source, is_main=False, is_deleted=False):
    return {
        "name": (
            "projects/dc-nav/databases/(default)/documents/rooms/"
            f"room1234567890123456/messages/{message_id}"
        ),
        "fields": {
            "source": {"stringValue": source},
            "createdAt": {"timestampValue": "2026-11-03T13:00:00Z"},
            "authorUser": {"mapValue": {"fields": {
                "displayName": {"stringValue": "Bea"},
                "userID": {"integerValue": "42"},
                "userName": {"stringValue": "bea/../nope"},
            }}},
            "flags": {"mapValue": {"fields": {
                "isMain": {"booleanValue": is_main},
                "isDeleted": {"booleanValue": is_deleted},
            }}},
        },
    }


def test_discussion_query_matches_the_sites_browse():
    query = dynamite.discussion_query(25)
    filters = query["where"]["compositeFilter"]["filters"]
    assert [item["fieldFilter"]["field"]["fieldPath"] for item in filters] == [
        "type", "flags.isEnabled", "flags.isArchived", "flags.isBrowsable", "flags.isDCB",
    ]
    assert filters[0]["fieldFilter"]["value"] == {"stringValue": "discussion"}
    assert filters[1]["fieldFilter"]["value"] == {"booleanValue": True}
    assert filters[4]["fieldFilter"]["value"] == {"booleanValue": False}
    assert [item["field"]["fieldPath"] for item in query["orderBy"]] == [
        "flags.isPinnedGlobal", "lastActivityAt",
    ]
    assert query["limit"] == 25
    assert query["from"] == [{"collectionId": "rooms"}]


def test_records_strip_html_skip_deleted_and_the_opening_message():
    _room_id, fields = dynamite.decode_document(_room())
    messages = [
        dynamite.decode_document(_message("room1234567890123456_main", source="<p>same</p>", is_main=True)),
        dynamite.decode_document(_message("gone", source="<p>deleted</p>", is_deleted=True)),
        dynamite.decode_document(_message("msg1", source="<p>I can help. Call 555-123-4567</p>")),
    ]
    records = dynamite.records_from_room(
        _room_id, fields, messages, excluded=["email_addresses", "phone_numbers"],
    )
    assert [row["url"] for row in records] == [
        "https://dc.dynamitecircle.com/room/room1234567890123456/discussion",
        "https://dc.dynamitecircle.com/room/room1234567890123456/message/msg1/discussion",
    ]
    assert records[0]["content"] == "We are hiring a developer. Write [email removed]"
    assert records[0]["title"] == "Need a React engineer"
    assert records[0]["author"] == {
        "display_name": "Ada",
        "source_author_id": "user-1",
        "profile_url": "https://dc.dynamitecircle.com/profile/ada",
    }
    assert records[0]["thread_id"] == "room1234567890123456"
    assert records[0]["published_at"] == datetime(2026, 11, 3, 12, 0)
    assert "[phone removed]" in records[1]["content"]
    assert records[1]["author"]["source_author_id"] == "42"
    assert records[1]["author"]["profile_url"] is None


def test_hidden_deleted_and_old_rooms_produce_nothing():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc).replace(tzinfo=None)
    for flag in ("isDeleted", "isSunk", "isHidden"):
        _room_id, fields = dynamite.decode_document(_room(flags={"mapValue": {"fields": {
            flag: {"booleanValue": True},
        }}}))
        assert dynamite.records_from_room(_room_id, fields, [], now=now) == []
    _room_id, fields = dynamite.decode_document(_room(
        createdAt={"timestampValue": "2020-01-01T00:00:00Z"},
    ))
    assert dynamite.records_from_room(
        _room_id, fields, [], max_age_days=30, now=now,
    ) == []
    assert dynamite.records_from_room(
        _room_id, fields, [], max_age_days=0, now=now,
    )


def test_session_roundtrip_plaintext_stays_readable_when_a_key_appears(tmp_path, monkeypatch):
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    db = _db(tmp_path)
    dynamite.save_session(db, "test-key", "test-refresh")
    stored = get_setting(db, dynamite.SESSION_KEY)
    assert stored.startswith("plain:")
    assert dynamite.load_session(db) == {"api_key": "test-key", "refresh_token": "test-refresh"}

    monkeypatch.setenv("CIRCLE_CRED_KEY", "unit-test-passphrase")
    assert dynamite.load_session(db)["refresh_token"] == "test-refresh"
    dynamite.save_session(db, "test-key", "rotated")
    sealed = get_setting(db, dynamite.SESSION_KEY)
    assert not sealed.startswith("plain:")
    assert "rotated" not in sealed
    assert dynamite.load_session(db)["refresh_token"] == "rotated"


def test_refresh_sends_the_site_referer_and_hides_the_token():
    seen = {}

    class Http:
        def post(self, url, params=None, data=None, headers=None, timeout=None, **_kw):
            seen.update(url=url, params=params, data=data, headers=headers)
            return _Response(400, {"error": {"message": "INVALID_REFRESH_TOKEN"}})

    try:
        dynamite.refresh_access(Http(), "test-key", "test-refresh")
    except dynamite.SessionInvalid as exc:
        assert "test-refresh" not in str(exc)
        assert "test-key" not in str(exc)
    else:
        raise AssertionError("expected SessionInvalid")
    assert seen["headers"]["Referer"] == "https://dc.dynamitecircle.com/"
    assert seen["headers"]["Origin"] == "https://dc.dynamitecircle.com"
    assert seen["data"]["grant_type"] == "refresh_token"
    assert seen["params"]["key"] == "test-key"


def test_a_plaintext_session_is_sealed_when_the_key_is_present(tmp_path, monkeypatch):
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    db = _db(tmp_path)
    dynamite.save_session(db, "test-key", "test-refresh")
    monkeypatch.setenv("CIRCLE_CRED_KEY", "unit-test-passphrase")
    now = datetime.now(timezone.utc).isoformat()
    set_setting(db, dynamite.KEY_LAST_RUN, now)
    set_setting(db, "dynamite_last_finish", now)

    class Http:
        def post(self, *args, **kwargs):
            raise AssertionError("network")

    assert dynamite.run_scheduled(db, Requirements(), http=Http()) is None
    sealed = get_setting(db, dynamite.SESSION_KEY)
    assert not sealed.startswith("plain:")
    assert "test-refresh" not in sealed
    assert dynamite.load_session(db)["refresh_token"] == "test-refresh"


def test_a_missing_session_or_a_recent_read_does_nothing(tmp_path):
    db = _db(tmp_path)

    class Http:
        def post(self, *args, **kwargs):
            raise AssertionError("network")

    assert dynamite.run_scheduled(db, Requirements(), http=Http()) is None
    dynamite.save_session(db, "test-key", "test-refresh")
    set_setting(db, dynamite.KEY_LAST_RUN, datetime.now(timezone.utc).isoformat())
    set_setting(db, "dynamite_last_finish", datetime.now(timezone.utc).isoformat())
    assert dynamite.run_scheduled(db, Requirements(), http=Http()) is None


def test_run_scheduled_ingests_and_keeps_the_rotated_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    db = _db(tmp_path)
    dynamite.save_session(db, "test-key", "test-refresh")
    seen = {}

    class Http:
        def post(self, url, **kwargs):
            if "securetoken" in url:
                return _Response(200, {"id_token": "id-token", "refresh_token": "rotated"})
            if url.endswith("/documents:runQuery"):
                return _Response(200, [{"document": _room()}])
            return _Response(400, {})

        def get(self, url, **kwargs):
            assert url.endswith("/rooms/room1234567890123456/messages")
            return _Response(200, {"documents": [
                _message("room1234567890123456_main", source="<p>same</p>", is_main=True),
                _message("msg1", source="<p>Happy to intro a developer.</p>"),
            ]})

    def fake_triage(_db, records, _requirements, **kwargs):
        seen["records"] = records
        seen["kwargs"] = kwargs
        return TriageResult(total_posts=len(records), leads=[{"id": 1}])

    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", fake_triage)
    # run_scheduled imports triage_records inside the function.
    result = dynamite.run_scheduled(db, Requirements(), http=Http(), force=True, use_llm=False)
    assert result["rooms"] == 1
    assert result["records"] == 2
    assert result["leads"] == 1
    assert seen["kwargs"]["community"] == dynamite.HOST
    assert seen["kwargs"]["source_url"] == dynamite.ORIGIN
    assert dynamite.load_session(db)["refresh_token"] == "rotated"
    with db.session() as s:
        community = s.query(Community).filter_by(host=dynamite.HOST).one()
        assert community.platform == "dynamite"
        assert community.name == "Dynamite Circle"
    stored = json.loads(get_setting(db, "dynamite_last_result"))
    assert stored["leads"] == 1


def test_an_existing_blank_platform_is_marked_dynamite(tmp_path, monkeypatch):
    db = _db(tmp_path)
    with db.session() as s:
        from circle_leads.storage.database import get_or_create_community
        get_or_create_community(s, slug=dynamite.HOST, url=dynamite.ORIGIN, name="Old")
    dynamite._ensure_community(db)
    with db.session() as s:
        community = s.query(Community).one()
        assert community.platform == "dynamite"
        assert community.name == "Old"


def test_runtime_lists_the_dynamite_stage(tmp_path, monkeypatch):
    from tests.dash_fixtures import make_app, setting

    client, db, _app = make_app(tmp_path, monkeypatch)
    with db.session() as s:
        setting(s, "dynamite_last_run", "2026-11-04T07:00:00")
        setting(s, "dynamite_last_result", json.dumps({"rooms": 2, "leads": 1}))
    stage = client.get("/api/dash/runtime").json()["stages"]["dynamite"]
    assert stage["schedule"] == "every 2 h"
    assert stage["last_run"] == "2026-11-04T07:00:00Z"
    assert stage["last_result"] == {"rooms": 2, "leads": 1}
    assert "harvest" in client.get("/api/dash/schedule").json()
    assert "dynamite" not in client.get("/api/dash/schedule").json()


def test_decode_value_covers_the_firestore_scalars():
    assert dynamite.decode_value({"integerValue": "7"}) == 7
    assert dynamite.decode_value({"nullValue": None}) is None
    assert dynamite.decode_value({"arrayValue": {"values": [
        {"stringValue": "a"}, {"booleanValue": False},
    ]}}) == ["a", False]
