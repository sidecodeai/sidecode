"""Account rules: what a username may be, what a password may be, and how one is stored (A24).

One module owns all three, so a rule written here is the rule everywhere: the register route, the
admin routes and the password change all validate through these functions rather than repeating a
regular expression each.

A password is hashed with scrypt and stored as
``scrypt$<n>$<r>$<p>$<salt base64>$<hash base64>``. The parameters travel with the hash, so raising
the cost later still leaves every older row readable. Verification is constant time against the
derived key, and `verify_dummy` runs the same work against a throwaway hash so a login for an
account that does not exist costs what a real one costs.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import os
import re
import secrets

#: The operator's account. Its password is ``RC_PASSWORD`` and is never stored.
ADMIN_USERNAME = "admin"

#: Protocol §3.1. Lower case, 3 to 32 characters, starting with a letter or a digit.
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = frozenset({ROLE_ADMIN, ROLE_MEMBER})

STATE_ACTIVE = "active"
STATE_DISABLED = "disabled"
STATES = frozenset({STATE_ACTIVE, STATE_DISABLED})

SCHEME = "scrypt"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_BYTES = 32
SALT_BYTES = 16


def normalize_username(value: str) -> str:
    """Lower-case and trim what a client sent, so ``Alice`` and ``alice`` are one account."""
    return value.strip().lower()


def username_allowed(value: str) -> bool:
    return USERNAME_PATTERN.fullmatch(value) is not None


def password_allowed(value: str) -> bool:
    return MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH


def role_allowed(value: str) -> bool:
    return value in ROLES


def state_allowed(value: str) -> bool:
    return value in STATES


async def hash_password(password: str) -> str:
    """Derive a storable hash. Off the event loop: scrypt is deliberately expensive."""
    return await asyncio.to_thread(hash_password_sync, password)


def hash_password_sync(password: str) -> str:
    salt = os.urandom(SALT_BYTES)
    derived = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        [
            SCHEME,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        ]
    )


async def verify_hash(password: str, encoded: str | None) -> bool:
    return await asyncio.to_thread(verify_hash_sync, password, encoded)


def verify_hash_sync(password: str, encoded: str | None) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return False
    salt, expected, cost, block_size, parallelism = parsed
    derived = _derive(password, salt, cost, block_size, parallelism)
    return hmac.compare_digest(derived, expected)


async def verify_dummy(password: str) -> bool:
    """Spend one verification against a hash nothing can match.

    Without this, a login for a name no account has would return before scrypt ran, and the
    difference would enumerate which usernames exist. Always False.
    """
    return await asyncio.to_thread(verify_hash_sync, password, _dummy_hash())


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """One unmatchable hash per process, derived from a secret this process then forgets."""
    return hash_password_sync(secrets.token_urlsafe(32))


def _derive(password: str, salt: bytes, cost: int, block_size: int, parallelism: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=cost,
        r=block_size,
        p=parallelism,
        dklen=SCRYPT_KEY_BYTES,
        maxmem=_max_memory(cost, block_size, parallelism),
    )


def _max_memory(cost: int, block_size: int, parallelism: int) -> int:
    """OpenSSL refuses a derivation past its own 32 MiB default, so state the budget it needs."""
    return 256 * cost * block_size + 1024 * block_size * parallelism + 1024 * 1024


def _parse(encoded: str | None) -> tuple[bytes, bytes, int, int, int] | None:
    if not encoded:
        return None
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    try:
        cost, block_size, parallelism = (int(item) for item in parts[1:4])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except ValueError:
        return None
    if cost < 2 or block_size < 1 or parallelism < 1 or not salt or not expected:
        return None
    return salt, expected, cost, block_size, parallelism
