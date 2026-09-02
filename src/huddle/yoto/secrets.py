"""Encryption for OAuth tokens at rest.

A Yoto refresh token is a durable key to somebody's account. Huddle stores one
per subscriber because unattended daily publishing requires it, so the database
is worth protecting on its own terms rather than trusting the filesystem.

When ``HUDDLE_TOKEN_ENCRYPTION_KEY`` is set the values are Fernet-encrypted
(AES-128-CBC with an HMAC tag) before they reach a column. When it is not, they
are stored as-is and a warning is logged once -- acceptable on a laptop, not in
production. Because ciphertext carries a version prefix, :func:`decrypt` reads
rows written under either mode, so turning encryption on needs no migration.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENV_KEY = "HUDDLE_TOKEN_ENCRYPTION_KEY"
#: Marks a value this module wrote. Anything without it is legacy plaintext.
PREFIX = "enc:v1:"

_warned = False


class TokenDecryptionError(RuntimeError):
    """The stored ciphertext will not open with the configured key.

    Almost always a rotated or mistyped key. Callers treat it as "this
    subscriber must reconnect" rather than crashing the run for everybody.
    """


def generate_key() -> str:
    """A fresh key, printable and safe to paste into an environment file."""
    return Fernet.generate_key().decode()


@lru_cache(maxsize=1)
def _cipher() -> Fernet | None:
    raw = os.environ.get(ENV_KEY, "").strip()
    if not raw:
        return None
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        # Accept a human-chosen passphrase too, rather than refusing to start
        # and tempting the operator to turn encryption off entirely.
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def reset_cipher_cache() -> None:
    """Test hook: re-read the key from the environment."""
    _cipher.cache_clear()
    global _warned
    _warned = False


def encryption_enabled() -> bool:
    return _cipher() is not None


def encrypt(value: str | None) -> str | None:
    global _warned
    if value is None:
        return None
    cipher = _cipher()
    if cipher is None:
        if not _warned:
            logger.warning(
                "%s is not set: Yoto tokens are being stored in plaintext. "
                "Set it before running Huddle for anyone but yourself.",
                ENV_KEY,
            )
            _warned = True
        return value
    return PREFIX + cipher.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(PREFIX):
        # Written before encryption was enabled. Readable, and rewritten as
        # ciphertext the next time the token is refreshed.
        return value
    cipher = _cipher()
    if cipher is None:
        raise TokenDecryptionError(
            f"stored token is encrypted but {ENV_KEY} is not set"
        )
    try:
        return cipher.decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise TokenDecryptionError("stored token does not open with the configured key") from exc


def redact(value: str | None) -> str:
    """A stable, non-reversible label for logs and dashboards."""
    if not value:
        return "<none>"
    return f"…{value[-4:]} ({hashlib.sha256(value.encode()).hexdigest()[:8]})"
