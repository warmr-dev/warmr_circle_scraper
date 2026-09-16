"""Tests for the Warmr portal community intake sync."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from circle_leads.export.community_intake import (
    DEFAULT_INTAKE_URL,
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
    fake.post.return_value = MagicMock(status_code=200, text="ok", reason="OK")

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

    with patch("circle_leads.export.community_intake.post_communities") as mock_post:
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
    with patch("circle_leads.export.community_intake.post_communities"):
        push_monitored_communities(db)

    with db.session() as s:
        c = s.query(Community).filter_by(slug="watched").one()
        c.price_label = "$150/mo"

    with patch("circle_leads.export.community_intake.post_communities") as mock_post:
        res = push_monitored_communities(db)
        assert res.sent == 1
        assert mock_post.call_args.args[0][0]["slug"] == "watched"
        assert mock_post.call_args.args[0][0]["entry_cost"] == 150


def test_push_force_resends_all(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("COMMUNITY_INTAKE_API_SECRET", "sekrit")
    with patch("circle_leads.export.community_intake.post_communities"):
        push_monitored_communities(db)
    with patch("circle_leads.export.community_intake.post_communities") as mock_post:
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
    with patch("circle_leads.export.community_intake.post_communities") as mock_post:
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
    with patch("circle_leads.export.community_intake.post_communities") as mock_post:
        h.harvest(db, dev_requirements, search=False)
    assert mock_post.called
