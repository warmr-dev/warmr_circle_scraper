"""Tests for the Warmr portal community intake sync."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from circle_leads.export.community_intake import (
    DEFAULT_INTAKE_URL,
    IntakeBatchResult,
    SOURCE,
    collect_monitored_communities,
    load_community_intake_config,
    parse_entry_cost,
    post_communities,
    push_monitored_communities,
)
from circle_leads.storage.database import Database
from circle_leads.storage.models import (
    CircleConnection,
    Community,
    ConnectionPriority,
    PermissionStatus,
    ReplaySession,
)


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/intake.db")


def _stored_session(host: str) -> ReplaySession:
    return ReplaySession(host=host, encrypted_cookies="plain:[]", cookie_count=0)


def _seed(db: Database) -> None:
    with db.session() as s:
        s.add(Community(
            slug="watched", name="Watched Co", url="https://watched.circle.so",
            platform="circle", watching=True, price_label="$99/mo",
        ))
        s.add(Community(
            slug="approved", name="Approved Co", url="https://approved.circle.so",
            platform="circle",
            permission_status=PermissionStatus.APPROVED.value,
            price_label="Free",
        ))
        # Neither watched nor approved -> not monitored.
        s.add(Community(
            slug="candidate", name="Candidate Co",
            url="https://candidate.circle.so", platform="circle",
        ))
        # Watched but not a Circle platform -> not readable, not monitored.
        s.add(Community(
            slug="fb", name="FB Group",
            url="https://www.facebook.com/groups/x", platform="facebook",
            watching=True,
        ))
        s.add(CircleConnection(
            host="private.circle.so", name="Private Co",
            priority=ConnectionPriority.NORMAL.value,
        ))
        s.add(CircleConnection(
            host="paused.circle.so", name="Paused Co",
            priority=ConnectionPriority.PAUSED.value,
        ))
        # Connection without a stored session -> not monitored.
        s.add(CircleConnection(
            host="nosession.circle.so", name="No Session Co",
            priority=ConnectionPriority.NORMAL.value,
        ))
        s.add(_stored_session("private.circle.so"))
        s.add(_stored_session("paused.circle.so"))


def _confirmed_post(items, **kwargs):
    return IntakeBatchResult(confirmed_indices=list(range(len(items))))


def _receipt(items, statuses=None):
    statuses = statuses or ["candidate_created"] * len(items)
    rows = []
    for index, (item, status) in enumerate(zip(items, statuses)):
        row = {"item_index": index, "status": status}
        if status == "error":
            row.update(code="invalid_payload", error="Invalid community slug")
        else:
            row.update(name=item["name"], candidate_id=f"00000000-0000-4000-8000-{index + 1:012d}")
        rows.append(row)
    return {
        "ok": True, "received": len(items),
        "accepted": statuses.count("candidate_created"),
        "existing": statuses.count("existing"),
        "rejected": statuses.count("error"), "results": rows,
    }


def _http_response(body, status=202):
    response = MagicMock(status_code=status, text="", reason="Accepted")
    response.json.return_value = body
    return response


# --- config --------------------------------------------------------------


def test_load_config_default_url(monkeypatch):
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "sekrit")
    monkeypatch.delenv("COMMUNITY_INTAKE_URL", raising=False)
    cfg = load_community_intake_config()
    assert cfg.enabled
    assert cfg.url == DEFAULT_INTAKE_URL
    assert cfg.api_secret == "sekrit"


def test_config_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("COMMUNITY_INTAKE_API_SECRET", raising=False)
    assert not load_community_intake_config().enabled


def test_config_url_override(monkeypatch):
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "s")
    monkeypatch.setenv("COMMUNITY_INTAKE_URL", "https://staging.test/intake")
    assert load_community_intake_config().url == "https://staging.test/intake"


# --- price parsing -----------------------------------------------------


@pytest.mark.parametrize("label,expected", [
    ("$49/mo", 49),
    ("$1,200/yr", 1200),
    ("USD 499", 499),
    ("Free", 0),
    ("free to join", 0),
    (None, None),
    ("", None),
    ("members only", None),
])
def test_parse_entry_cost(label, expected):
    assert parse_entry_cost(label) == expected


# --- collecting the monitored set ------------------------------------


def test_collect_monitored_communities(db):
    _seed(db)
    with db.session() as s:
        got = {mc.key: mc.payload for mc in collect_monitored_communities(s)}

    assert set(got) == {
        "community:watched",
        "community:approved",
        "connection:private.circle.so",
    }

    watched = got["community:watched"]
    assert watched["name"] == "Watched Co"
    assert watched["slug"] == "watched"
    assert watched["platform"] == "circle"
    assert watched["join_link"] == "https://watched.circle.so"
    assert watched["entry_cost"] == 99
    assert watched["is_paid_membership"] is True
    assert watched["is_active"] is True
    assert watched["source"] == SOURCE

    approved = got["community:approved"]
    assert approved["entry_cost"] == 0
    assert approved["is_paid_membership"] is False

    private = got["connection:private.circle.so"]
    assert private["visibility"] == "private"
    assert private["join_link"] == "https://private.circle.so"
    assert "entry_cost" not in private


def test_connection_deduped_against_public_row(db):
    with db.session() as s:
        s.add(Community(
            slug="shared", name="Shared Co", url="https://shared.circle.so",
            platform="circle", watching=True,
        ))
        s.add(CircleConnection(host="shared.circle.so", name="Shared Co (private)"))
        s.add(_stored_session("shared.circle.so"))
    with db.session() as s:
        keys = [mc.key for mc in collect_monitored_communities(s)]
    assert keys == ["community:shared"]


# --- POST shape ------------------------------------------------------


def test_post_sends_secret_header():
    from circle_leads.export.community_intake import CommunityIntakeConfig

    cfg = CommunityIntakeConfig(url="https://x.test/intake", api_secret="sekrit")
    fake = MagicMock()
    fake.post.return_value = _http_response(_receipt([{"name": "A"}]))

    post_communities([{"name": "A", "join_link": "https://a.circle.so"}],
                     config=cfg, session=fake)

    fake.post.assert_called_once()
    args, kwargs = fake.post.call_args
    assert args[0] == cfg.url
    assert isinstance(kwargs["json"], list)
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["x-community-intake-secret"] == "sekrit"


def test_post_raises_on_http_error():
    from circle_leads.export.community_intake import CommunityIntakeConfig

    cfg = CommunityIntakeConfig(url="https://x.test/intake", api_secret="s")
    fake = MagicMock()
    fake.post.return_value = MagicMock(status_code=422, text="bad", reason="Unprocessable")
    with pytest.raises(RuntimeError, match="HTTP 422"):
        post_communities([{"name": "A", "join_link": "u"}], config=cfg, session=fake)


# --- push + sync-state cache ----------------------------------------


def test_push_skipped_without_config(db, monkeypatch):
    _seed(db)
    monkeypatch.delenv("COMMUNITY_INTAKE_API_SECRET", raising=False)
    with patch("circle_leads.export.community_intake.post_communities") as mock_post:
        result = push_monitored_communities(db)
    mock_post.assert_not_called()
    assert result.sent == 0 and result.attempted == 0


def test_push_sends_then_caches(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "sekrit")

    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post) as mock_post:
        first = push_monitored_communities(db)
        assert first.sent == 3
        assert mock_post.call_count == 1
        batch = mock_post.call_args.args[0]
        assert {c["slug"] for c in batch} == {"watched", "approved", "private"}

    # Nothing changed -> second run is a no-op.
    with patch("circle_leads.export.community_intake.post_communities") as mock_post:
        again = push_monitored_communities(db)
        mock_post.assert_not_called()
        assert again.sent == 0
        assert again.skipped == 3


def test_push_resends_only_changed(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "sekrit")
    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post):
        push_monitored_communities(db)

    with db.session() as s:
        c = s.query(Community).filter_by(slug="watched").one()
        c.price_label = "$150/mo"

    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post) as mock_post:
        res = push_monitored_communities(db)
        assert res.sent == 1
        assert mock_post.call_args.args[0][0]["slug"] == "watched"
        assert mock_post.call_args.args[0][0]["entry_cost"] == 150


def test_push_force_resends_all(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "sekrit")
    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post):
        push_monitored_communities(db)
    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post) as mock_post:
        res = push_monitored_communities(db, force=True)
        assert res.sent == 3
        mock_post.assert_called_once()


def test_push_error_is_reported_not_raised(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "sekrit")
    with patch("circle_leads.export.community_intake.post_communities",
               side_effect=RuntimeError("boom")):
        res = push_monitored_communities(db)
    assert res.sent == 0
    assert res.errors and "boom" in res.errors[0]
    # A failed push is not cached, so the next run retries.
    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post) as mock_post:
        push_monitored_communities(db)
        assert mock_post.call_count == 1


def test_harvest_registers_communities(db, dev_requirements, monkeypatch):
    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "sekrit")
    import circle_leads.harvest as h

    class EmptyReader:
        def __init__(self, host, **kw):
            self.base = f"https://{host}"

        def list_spaces(self):
            return []

    monkeypatch.setattr(h, "PublicReader", EmptyReader)
    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post) as mock_post:
        h.harvest(db, dev_requirements, search=False)
    assert mock_post.called


def test_mixed_receipt_caches_only_confirmed_items_and_retries_rejection(db, monkeypatch):
    from circle_leads.storage.settings_store import get_setting

    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "fixture")
    http = MagicMock()

    def mixed(url, *, json, **kwargs):
        return _http_response(_receipt(json, ["candidate_created", "error", "existing"]))

    http.post.side_effect = mixed
    result = push_monitored_communities(db, session=http)
    assert result.attempted == 3
    assert result.sent == 2
    assert len(result.errors) == 1
    first_batch = http.post.call_args.kwargs["json"]
    rejected_slug = first_batch[1]["slug"]
    cache = json.loads(get_setting(db, "community_intake_state"))
    assert len(cache) == 2
    assert "community:" + rejected_slug not in cache

    http.reset_mock()
    http.post.side_effect = lambda url, *, json, **kw: _http_response(_receipt(json))
    retry = push_monitored_communities(db, session=http)
    assert retry.attempted == retry.sent == 1
    assert retry.skipped == 2
    assert not retry.errors
    assert http.post.call_args.kwargs["json"][0]["slug"] == rejected_slug


def test_202_all_rejected_is_not_sent_or_cached(db, monkeypatch):
    from circle_leads.storage.settings_store import get_setting

    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "fixture")
    http = MagicMock()
    http.post.side_effect = lambda url, *, json, **kw: _http_response(
        _receipt(json, ["error"] * len(json)),
    )
    result = push_monitored_communities(db, session=http)
    assert result.attempted == 3 and result.sent == 0
    assert len(result.errors) == 3
    assert get_setting(db, "community_intake_state") is None


def test_forced_rejection_removes_old_fingerprints(db, monkeypatch):
    from circle_leads.storage.settings_store import get_setting

    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "fixture")
    with patch("circle_leads.export.community_intake.post_communities", side_effect=_confirmed_post):
        push_monitored_communities(db)
    http = MagicMock()
    http.post.side_effect = lambda url, *, json, **kw: _http_response(
        _receipt(json, ["error"] * len(json)),
    )
    failed = push_monitored_communities(db, session=http, force=True)
    assert failed.sent == 0
    assert get_setting(db, "community_intake_state") == "{}"
    http.reset_mock()
    http.post.side_effect = lambda url, *, json, **kw: _http_response(_receipt(json))
    retried = push_monitored_communities(db, session=http)
    assert retried.sent == 3 and retried.skipped == 0


@pytest.mark.parametrize("failure", [
    "html", "not_object", "ok_false", "missing_result", "duplicate_index",
    "out_of_range_index", "boolean_index", "unknown_status", "missing_candidate",
    "invalid_candidate", "wrong_name", "bad_counts", "boolean_count", "status_counts",
])
def test_unverifiable_receipt_never_caches_batch(db, monkeypatch, failure):
    from circle_leads.storage.settings_store import get_setting

    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "fixture")
    http = MagicMock()

    def invalid(url, *, json, **kwargs):
        body = _receipt(json)
        response = _http_response(body)
        row = body["results"][0]
        if failure == "html":
            response.json.side_effect = ValueError("not JSON")
        elif failure == "not_object":
            response.json.return_value = []
        elif failure == "ok_false":
            body["ok"] = False
        elif failure == "missing_result":
            body["results"].pop()
        elif failure == "duplicate_index":
            row["item_index"] = 1
        elif failure == "out_of_range_index":
            row["item_index"] = len(json)
        elif failure == "boolean_index":
            row["item_index"] = False
        elif failure == "unknown_status":
            row["status"] = "queued"
        elif failure == "missing_candidate":
            row.pop("candidate_id")
        elif failure == "invalid_candidate":
            row["candidate_id"] = "not-a-uuid"
        elif failure == "wrong_name":
            row["name"] = "Another community"
        elif failure == "bad_counts":
            body["received"] += 1
        elif failure == "boolean_count":
            body["rejected"] = False
        elif failure == "status_counts":
            body.update(accepted=2, existing=1)
        return response

    http.post.side_effect = invalid
    result = push_monitored_communities(db, session=http)
    assert result.attempted == 3 and result.sent == 0
    assert result.errors
    assert get_setting(db, "community_intake_state") is None


def test_receipts_are_matched_by_index_and_normalized_name():
    items = [{"name": "A & Co"}, {"name": "Second"}]
    body = _receipt(items, ["existing", "candidate_created"])
    body["results"][0]["name"] = "a-co"
    body["results"].reverse()
    http = MagicMock()
    http.post.return_value = _http_response(body)
    from circle_leads.export.community_intake import CommunityIntakeConfig

    result = post_communities(items, config=CommunityIntakeConfig("https://x.test", "fixture"), session=http)
    assert sorted(result.confirmed_indices) == [0, 1]
    assert not result.errors


def test_later_batch_failure_preserves_earlier_confirmations(db, monkeypatch):
    from circle_leads.storage.settings_store import get_setting

    with db.session() as s:
        for i in range(12):
            s.add(Community(
                slug=f"batch-{i:02}", name=f"Community {i}",
                url=f"https://batch-{i:02}.circle.so", platform="circle", watching=True,
            ))
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "fixture")
    http = MagicMock()

    def reply(url, *, json, **kwargs):
        if len(json) == 10:
            return _http_response(_receipt(json))
        return _http_response({"ok": True})  # No per-item confirmation.

    http.post.side_effect = reply
    result = push_monitored_communities(db, session=http)
    assert result.attempted == 12 and result.sent == 10
    assert result.errors
    assert len(json.loads(get_setting(db, "community_intake_state"))) == 10
    http.reset_mock()
    http.post.side_effect = lambda url, *, json, **kw: _http_response(_receipt(json))
    retry = push_monitored_communities(db, session=http)
    assert retry.attempted == retry.sent == 2
    assert retry.skipped == 10
    assert {r["slug"] for r in http.post.call_args.kwargs["json"]} == {"batch-10", "batch-11"}


def test_harvest_activity_reports_confirmed_count_and_rejections(db, monkeypatch):
    from circle_leads.harvest import _register_monitored_communities
    from circle_leads.storage.models import ActivityLog

    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "fixture")
    http = MagicMock()
    http.post.side_effect = lambda url, *, json, **kw: _http_response(
        _receipt(json, ["candidate_created", "error", "existing"]),
    )
    with patch("circle_leads.export.community_intake.requests.Session", return_value=http):
        _register_monitored_communities(db)
    with db.session() as s:
        activity = s.query(ActivityLog).filter_by(kind="export").one()
        assert activity.level == "error"
        assert activity.detail["sent"] == 2
        assert activity.detail["attempted"] == 3
        assert "Invalid community slug" in activity.detail["errors"]
