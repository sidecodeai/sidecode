"""Session tokens and password authentication.

Adapted from cc-remote's ``cc_remote/relay/auth.py`` (MIT, see THIRD_PARTY_NOTICES.md). The token is
``base64url(payload).base64url(HMAC-SHA256(payload))`` rather than a JWT: no algorithm negotiation,
no library, nothing to confuse. ``sub`` is the account the token belongs to (A24): every device,
session and push registration the token reaches is that account's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from .accounts import ADMIN_USERNAME, normalize_username, verify_dummy, verify_hash
from .users import UserRecord, UserStore

SESSION_COOKIE_NAME = "rc_session"

#: `authenticate_login` outcomes other than a signed-in account. They are the error codes
#: `POST /api/login` answers with, so the route maps them straight to 401 and 403.
LOGIN_UNAUTHORIZED = "unauthorized"
LOGIN_FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class SessionClaims:
    username: str
    expires_at: int
    jti: str


def verify_password(password: str, expected: str) -> bool:
    """Constant-time comparison that fails closed when no password is configured."""
    if not expected or not password:
        return False
    return hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))


async def authenticate_login(
    users: UserStore, username: str, password: str, admin_password: str
) -> UserRecord | str:
    """Return the signed-in account, or ``unauthorized`` / ``forbidden`` (A24).

    The password is checked before the account's state, so a wrong guess never reveals that the
    name belongs to a disabled account. An account that does not exist still costs one scrypt, so
    the time taken does not enumerate usernames either.
    """
    candidate = normalize_username(username)
    record = await users.get(candidate) if candidate else None
    if record is None:
        await verify_dummy(password)
        return LOGIN_UNAUTHORIZED
    if record.username == ADMIN_USERNAME:
        # The operator's password is the configured one; the dummy keeps the cost identical.
        correct = verify_password(password, admin_password)
        await verify_dummy(password)
    else:
        correct = await verify_hash(password, record.password_hash)
    if not correct:
        return LOGIN_UNAUTHORIZED
    if not record.active:
        return LOGIN_FORBIDDEN
    return record


def make_session_token(
    secret: str,
    ttl: int,
    *,
    username: str = ADMIN_USERNAME,
    now: float | None = None,
    jti: str | None = None,
) -> tuple[str, int]:
    """Sign a session token. Returns ``(token, expiry_epoch_seconds)``."""
    expires_at = int(time.time() if now is None else now) + ttl
    claims = {"exp": expires_at, "jti": jti or secrets.token_urlsafe(24), "sub": username}
    payload = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = _sign(secret, payload)
    return f"{payload}.{signature}", expires_at


def session_token_claims(token: str, secret: str) -> SessionClaims | None:
    """Return authentic claims, or ``None`` for a forged or malformed token.

    An expired but authentic token still yields claims; callers reject it against their own clock
    and against the revocation registry.
    """
    if not token or not secret or token.count(".") != 1:
        return None
    payload, signature = token.split(".", 1)
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(_pad(payload)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    expires_at, jti, username = data.get("exp"), data.get("jti"), data.get("sub")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        return None
    if not isinstance(jti, str) or not 16 <= len(jti) <= 128:
        return None
    if not isinstance(username, str) or not 1 <= len(username) <= 64:
        return None
    return SessionClaims(username=username, expires_at=expires_at, jti=jti)


def bearer_token(header_value: str | None) -> str:
    """Extract the credential from an ``Authorization: Bearer <token>`` header."""
    if not header_value or not header_value.lower().startswith("bearer "):
        return ""
    return header_value[7:].strip()


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _pad(value: str) -> bytes:
    return value.encode("ascii") + b"=" * (-len(value) % 4)
