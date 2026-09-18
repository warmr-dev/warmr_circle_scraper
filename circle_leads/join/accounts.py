"""Circle accounts the join bot can act as, each pinned to its own browser profile.

An ego-browser task space shares its browser profile's cookie jar with the
user's own tabs and with every other space on that profile (ego-browser's own
docs, references/clearing-state.md). Two accounts driven from spaces on the
same profile are therefore one browser identity: a host already signed in as
one of them is joined as that one, whatever account the batch was run for.
Confirmed on the 2026-09-18 test run, where both accounts' spaces were created
on the operator's personal profile. So every account names its own Ego Lite
profile, and an account without one is refused rather than run in whatever
profile happens to be the default.

Accounts are numbered 1..MAX_ACCOUNTS; "main" and "test" stay as aliases for 1
and 2 because the attempt log and the CLI already use those names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MAX_ACCOUNTS = 10
_ALIASES = {"main": 1, "test": 2}
_NAMES = {n: name for name, n in _ALIASES.items()}


@dataclass(frozen=True)
class JoinAccount:
    number: int

    @property
    def key(self) -> str:
        """The label written to the attempt log and to circle_connections."""
        return _NAMES.get(self.number, str(self.number))

    def _var(self, base: str) -> str:
        return base if self.number == 1 else f"{base}{self.number}"

    @property
    def email_var(self) -> str:
        return self._var("CIRCLE_EMAIL")

    @property
    def password_var(self) -> str:
        return self._var("CIRCLE_PASSWORD")

    @property
    def profile_var(self) -> str:
        return self._var("CIRCLE_EGO_PROFILE")

    @property
    def email(self) -> str | None:
        return os.environ.get(self.email_var) or None

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_var) or None

    @property
    def ego_profile(self) -> str | None:
        """Name (or id) of the Ego Lite profile this account runs in."""
        return (os.environ.get(self.profile_var) or "").strip() or None


def resolve_account(account: str | int) -> JoinAccount:
    """``"main"``, ``"test"`` or a number 1..MAX_ACCOUNTS (as int or str)."""
    raw = str(account).strip().lower()
    if raw in _ALIASES:
        return JoinAccount(_ALIASES[raw])
    if raw.isdigit() and 1 <= int(raw) <= MAX_ACCOUNTS:
        return JoinAccount(int(raw))
    raise ValueError(
        f"Unknown account {account!r} -- expected main, test or a number 1..{MAX_ACCOUNTS}."
    )


def account_choices() -> list[str]:
    return ["main", "test"] + [str(n) for n in range(1, MAX_ACCOUNTS + 1)]
