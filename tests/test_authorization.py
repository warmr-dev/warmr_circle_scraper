"""Tests for the authorization boundaries.

These are the constraints that keep the system inside what the account and
the operator actually permit. A failure here is a consent violation, not a bug.
"""

import os

import pytest

from circle_leads.authentication.browser_session import (
    AdminCredentials,
    MemberSession,
    MissingCredentialError,
    NotAuthorizedError,
    mint_member_session,
    redact,
    resolve_admin_credentials,
)
from circle_leads.config.settings import CommunityPermission
from circle_leads.pipeline import ingest_community
from circle_leads.scraper.community_scraper import (
    approved_chat_rooms,
    approved_spaces,
    is_direct_message_room,
)
from circle_leads.storage.database import Database


# --- Direct messages are never collected ------------------------------------


@pytest.mark.parametrize(
    "room",
    [
        {"uuid": "direct-1-2", "chat_room_kind": "direct"},
        {"uuid": "abc", "chat_room_kind": "direct"},
        {"uuid": "direct-5-9", "chat_room_kind": "group_chat"},  # uuid still says direct
        {"uuid": "x", "kind": "dm"},
    ],
)
def test_direct_message_rooms_are_rejected(room):
    assert is_direct_message_room(room) is True


def test_group_chat_rooms_are_allowed():
    assert is_direct_message_room({"uuid": "abc", "chat_room_kind": "group_chat"}) is False


@pytest.mark.parametrize(
    "room",
    [
        {"uuid": "abc"},                                  # no kind at all
        {"uuid": "abc", "chat_room_kind": "unknown_new"},  # unrecognized future kind
        {"uuid": "abc", "chat_room_kind": ""},
        {},
    ],
)
def test_unrecognized_room_kinds_fail_closed(room):
    """An allowlist must reject anything it cannot positively identify."""
    assert is_direct_message_room(room) is True


def test_approved_chat_rooms_filters_dms_even_if_uuid_allowlisted():
    perm = CommunityPermission(community_id="x", allowed_chat_room_uuids=["direct-1-2", "grp"])
    rooms = [
        {"uuid": "direct-1-2", "chat_room_kind": "direct"},
        {"uuid": "grp", "chat_room_kind": "group_chat"},
    ]
    result = approved_chat_rooms(rooms, perm)
    assert [r["uuid"] for r in result] == ["grp"]


# --- Space allowlisting -----------------------------------------------------


def test_empty_allowlist_collects_nothing():
    perm = CommunityPermission(community_id="x", allowed_space_ids=[])
    assert approved_spaces([{"id": 1}, {"id": 2}], perm) == []


def test_only_named_spaces_are_collected():
    perm = CommunityPermission(community_id="x", allowed_space_ids=["1", "general"])
    spaces = [{"id": 1, "slug": "a"}, {"id": 2, "slug": "general"}, {"id": 3, "slug": "z"}]
    assert {str(s["id"]) for s in approved_spaces(spaces, perm)} == {"1", "2"}


# --- Permission gating ------------------------------------------------------


@pytest.mark.parametrize("status", ["candidate", "contacted", "denied", "revoked"])
def test_unapproved_community_is_never_ingested(status, tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    from circle_leads.config.settings import load_requirements

    perm = CommunityPermission(community_id="x", permission_status=status)
    summary = ingest_community(db, perm, load_requirements())
    assert summary.items_seen == 0
    assert summary.state == "REQUIRES_MANUAL_ACTION"
    assert "approved" in summary.errors[0]


def test_resolve_admin_credentials_refuses_unapproved():
    perm = CommunityPermission(community_id="x", permission_status="candidate")
    with pytest.raises(NotAuthorizedError):
        resolve_admin_credentials(perm)


def test_missing_credential_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("CIRCLE_TEST_TOKEN", raising=False)
    perm = CommunityPermission(
        community_id="x", permission_status="approved",
        admin_token_env="CIRCLE_TEST_TOKEN",
    )
    with pytest.raises(MissingCredentialError):
        resolve_admin_credentials(perm)


def test_mint_member_session_refuses_unapproved():
    perm = CommunityPermission(community_id="x", permission_status="contacted")
    with pytest.raises(NotAuthorizedError):
        mint_member_session(perm)


# --- Credentials never leak -------------------------------------------------


def test_credentials_are_redacted_in_repr():
    creds = AdminCredentials(token="secret-token-abc123")
    assert "secret-token-abc123" not in repr(creds)

    member = MemberSession(access_token="jwt-secret-xyz", refresh_token="refresh-secret")
    assert "jwt-secret-xyz" not in repr(member)
    assert "refresh-secret" not in repr(member)


def test_redact_strips_sensitive_headers():
    out = redact({"Authorization": "Bearer abc", "Cookie": "s=1", "Accept": "json"})
    assert out["Authorization"] == "<redacted>"
    assert out["Cookie"] == "<redacted>"
    assert out["Accept"] == "json"


def test_secrets_are_read_from_env_not_stored(monkeypatch):
    monkeypatch.setenv("CIRCLE_TEST_TOKEN", "env-secret")
    perm = CommunityPermission(
        community_id="x", permission_status="approved",
        admin_token_env="CIRCLE_TEST_TOKEN",
    )
    # The permission model holds the variable NAME, never the value.
    assert "env-secret" not in perm.model_dump_json()
    assert perm.secret("CIRCLE_TEST_TOKEN") == "env-secret"
