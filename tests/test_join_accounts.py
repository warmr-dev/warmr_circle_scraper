"""Join accounts and the browser profile each one must run in (join/accounts.py)."""

from __future__ import annotations

import pytest

from circle_leads.join.accounts import account_choices, resolve_account


def test_main_and_test_are_accounts_one_and_two():
    assert resolve_account("main").number == 1
    assert resolve_account("1").key == "main"
    assert resolve_account("test").number == 2
    assert resolve_account(2).key == "test"


def test_account_one_keeps_the_unsuffixed_variables():
    main = resolve_account("main")
    assert (main.email_var, main.password_var, main.profile_var) == (
        "CIRCLE_EMAIL", "CIRCLE_PASSWORD", "CIRCLE_EGO_PROFILE")


def test_numbered_accounts_read_suffixed_variables(monkeypatch):
    monkeypatch.setenv("CIRCLE_EMAIL3", "three@example.com")
    monkeypatch.setenv("CIRCLE_PASSWORD3", "p3")
    monkeypatch.setenv("CIRCLE_EGO_PROFILE3", " erksh3 ")
    acct = resolve_account("3")
    assert acct.key == "3"
    assert acct.email == "three@example.com"
    assert acct.password == "p3"
    assert acct.ego_profile == "erksh3"


def test_an_account_without_a_profile_says_so(monkeypatch):
    monkeypatch.delenv("CIRCLE_EGO_PROFILE2", raising=False)
    assert resolve_account("test").ego_profile is None


@pytest.mark.parametrize("bad", ["0", "11", "prod", ""])
def test_unknown_accounts_are_rejected(bad):
    with pytest.raises(ValueError):
        resolve_account(bad)


def test_cli_choices_cover_the_aliases_and_ten_numbers():
    choices = account_choices()
    assert {"main", "test", "1", "10"} <= set(choices)
