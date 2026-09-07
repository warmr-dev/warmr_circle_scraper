"""Circle login credentials for the connector's automated login.

The user chose to store their Circle email + password rather than log in
interactively. This module makes that as safe as it can be given the choice:

- Credentials are read from the ENVIRONMENT (a gitignored `.env`), never from
  source and never hardcoded.
- They are held in memory only long enough to type into Circle's login page.
- They are never written to a log, never sent to the backend, and never put on
  the frontend. ``__repr__`` is redacted so an accidental print can't leak them.
- Optional at-rest encryption is supported for a stored form (see
  ``EncryptedStore``), with the honest caveat that a login step must decrypt to
  plaintext to type the password, so the key and ciphertext must not live in the
  same place for the encryption to mean anything.

Per-community env vars, so one login does not become a key to all of them:

    CIRCLE_EMAIL__<slug>     e.g. CIRCLE_EMAIL__yourspinstate
    CIRCLE_PASSWORD__<slug>

with a single-account fallback:

    CIRCLE_EMAIL / CIRCLE_PASSWORD
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass


def _slug(host: str) -> str:
    """Env-var-safe slug from a host: yourspinstate.com -> yourspinstate."""
    host = host.replace("https://", "").replace("http://", "").strip("/")
    label = host.split(".")[0] if "." in host else host
    # Communities like community.bigstarlights.com -> use the registrable label.
    if label in ("www", "community", "app", "members", "portal"):
        parts = host.split(".")
        label = parts[1] if len(parts) > 1 else label
    return re.sub(r"[^a-z0-9]", "_", label.lower())


@dataclass
class Credentials:
    email: str
    password: str

    def __repr__(self) -> str:  # never leak the password via an accidental print
        return f"Credentials(email={self.email!r}, password=<redacted>)"

    __str__ = __repr__


class CredentialsNotFound(RuntimeError):
    """No credentials in the environment for this host."""


def load_for_host(host: str, *, env: dict | None = None) -> Credentials:
    """Read this host's credentials from the environment.

    Raises CredentialsNotFound with a precise message naming the env vars, so
    the fix is obvious and the password itself never appears in the error.
    """
    env = env if env is not None else os.environ
    slug = _slug(host)
    email = env.get(f"CIRCLE_EMAIL__{slug}") or env.get("CIRCLE_EMAIL")
    password = env.get(f"CIRCLE_PASSWORD__{slug}") or env.get("CIRCLE_PASSWORD")
    if not email or not password:
        raise CredentialsNotFound(
            f"No Circle credentials for {host}. Set CIRCLE_EMAIL__{slug} and "
            f"CIRCLE_PASSWORD__{slug} (or CIRCLE_EMAIL / CIRCLE_PASSWORD) in your "
            f".env. They stay on this machine and are never uploaded."
        )
    return Credentials(email=email, password=password)


# --- optional at-rest encryption ------------------------------------------
#
# A convenience for keeping a stored credentials file encrypted rather than in
# plaintext. Uses AES-GCM via `cryptography` if available. The key comes from
# CIRCLE_CRED_KEY (a passphrase, stretched with scrypt). This does NOT make it
# safe to ship the password to a server: to log in, the connector must decrypt
# to plaintext locally, so this only protects a file at rest on your machine
# against someone who has the file but not the passphrase.


class EncryptionUnavailable(RuntimeError):
    """The `cryptography` package is not installed."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    # scrypt: memory-hard, resists brute force better than a bare hash.
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def encrypt(plaintext: str, passphrase: str) -> str:
    """Encrypt a string to a self-describing base64 token (salt|nonce|ct)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise EncryptionUnavailable(
            "Encryption needs the 'cryptography' package: pip install cryptography"
        ) from exc
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(salt + nonce + ct).decode("ascii")


def decrypt(token: str, passphrase: str) -> str:
    """Reverse :func:`encrypt`."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise EncryptionUnavailable(
            "Encryption needs the 'cryptography' package: pip install cryptography"
        ) from exc
    raw = base64.b64decode(token)
    salt, nonce, ct = raw[:16], raw[16:28], raw[28:]
    key = _derive_key(passphrase, salt)
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
