"""Circle login credentials: env resolution, redaction, at-rest encryption.

These guard the security properties the user's choice depends on: credentials
come only from the environment, never appear in a repr/str/log, and the
optional encryption actually round-trips.
"""

from __future__ import annotations

import pytest

from circle_leads.connector.credentials import (
    CredentialsNotFound, Credentials, _slug, decrypt, encrypt, load_for_host,
)


# --- slug + env resolution -------------------------------------------------

@pytest.mark.parametrize("host,slug", [
    ("yourspinstate.com", "yourspinstate"),
    ("www.yourspinstate.com", "yourspinstate"),
    ("community.bigstarlights.com", "bigstarlights"),
    ("https://altea.circle.so/", "altea"),
    ("members.example.io", "example"),
])
def test_slug_uses_the_registrable_label(host, slug):
    assert _slug(host) == slug


def test_per_community_vars_win_over_the_fallback():
    env = {
        "CIRCLE_EMAIL": "fallback@x.com", "CIRCLE_PASSWORD": "fallbackpw",
        "CIRCLE_EMAIL__yourspinstate": "me@spin.com",
        "CIRCLE_PASSWORD__yourspinstate": "spinpw",
    }
    c = load_for_host("www.yourspinstate.com", env=env)
    assert c.email == "me@spin.com" and c.password == "spinpw"


def test_fallback_is_used_when_no_per_community_var():
    env = {"CIRCLE_EMAIL": "me@x.com", "CIRCLE_PASSWORD": "pw"}
    c = load_for_host("community.bigstarlights.com", env=env)
    assert c.email == "me@x.com"


def test_missing_credentials_raise_a_clear_named_error():
    with pytest.raises(CredentialsNotFound) as exc:
        load_for_host("community.bigstarlights.com", env={})
    msg = str(exc.value)
    assert "CIRCLE_EMAIL__bigstarlights" in msg
    assert "never uploaded" in msg


# --- redaction (a password must never leak via print/log) ------------------

def test_password_is_redacted_in_repr_and_str():
    c = Credentials(email="me@x.com", password="hunter2-SECRET")
    assert "hunter2-SECRET" not in repr(c)
    assert "hunter2-SECRET" not in str(c)
    assert "<redacted>" in repr(c)
    # The email is not secret and stays visible for diagnostics.
    assert "me@x.com" in repr(c)


def test_password_not_in_the_missing_error_even_if_email_set():
    env = {"CIRCLE_EMAIL": "me@x.com"}  # password absent
    with pytest.raises(CredentialsNotFound) as exc:
        load_for_host("x.circle.so", env=env)
    assert "me@x.com" not in str(exc.value)  # error names vars, not values


# --- at-rest encryption ----------------------------------------------------

def _crypto_available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _crypto_available(), reason="cryptography not installed")
def test_encrypt_decrypt_round_trip():
    token = encrypt("s3cr3t-password", "a-strong-passphrase")
    assert "s3cr3t-password" not in token          # ciphertext, not plaintext
    assert decrypt(token, "a-strong-passphrase") == "s3cr3t-password"


@pytest.mark.skipif(not _crypto_available(), reason="cryptography not installed")
def test_encrypt_is_non_deterministic():
    a = encrypt("pw", "key")
    b = encrypt("pw", "key")
    assert a != b  # fresh salt + nonce each time


@pytest.mark.skipif(not _crypto_available(), reason="cryptography not installed")
def test_wrong_passphrase_fails_to_decrypt():
    token = encrypt("pw", "right-key")
    with pytest.raises(Exception):
        decrypt(token, "wrong-key")
