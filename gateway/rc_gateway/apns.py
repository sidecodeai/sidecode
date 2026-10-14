"""APNs provider: ES256 provider tokens over one pooled HTTP/2 connection.

Adapted from cc-remote's ``relay/native_push.py`` (MIT, see THIRD_PARTY_NOTICES.md). The parts kept
verbatim in spirit are the JWT cache, the fixed set of retryable failure reasons, and the logging
filters: an APNs request URL contains the device token, and HPACK debug logging would leak the
authorization header in a form no string redaction can clean.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .logging import logger

log = logger("rc_gateway.apns")

HOSTS = {
    "sandbox": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}
#: Failure reasons Apple documents; anything else is treated as a transport failure.
REASONS = frozenset(
    {
        "BadDeviceToken",
        "DeviceTokenNotForTopic",
        "Forbidden",
        "ExpiredToken",
        "Unregistered",
        "PayloadTooLarge",
        "TooManyProviderTokenUpdates",
        "TooManyRequests",
        "InternalServerError",
        "ServiceUnavailable",
        "Shutdown",
        "ExpiredProviderToken",
        "InvalidProviderToken",
        "BadTopic",
        "MissingTopic",
    }
)
#: Reasons that mean the token is dead and the registration must be deleted.
PERMANENT_REASONS = frozenset(
    {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered", "ExpiredToken"}
)
TOKEN_TTL_SECONDS = 50 * 60
MAX_PAYLOAD_BYTES = 4096
#: `apns-priority`. 10 delivers at once, which a notification someone reads needs; 5 lets the
#: phone's power budget decide, which is enough for one that only changes the badge (A47).
PRIORITY_IMMEDIATE = 10
PRIORITY_POWER_AWARE = 5

_APNS_HTTP: ContextVar[bool] = ContextVar("rc_gateway_apns_http", default=False)


class _RequestLogFilter(logging.Filter):
    """httpx logs request URLs at INFO; an APNs URL contains the device token."""

    _device_url = re.compile(
        r"(https://api(?:\.sandbox)?\.push\.apple\.com(?::443)?/3/device/)[^\s\"'<>]+",
        re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._device_url.sub(r"\1[redacted]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class _HeaderLogFilter(logging.Filter):
    """HPACK debug logging emits the encoded header block; redaction cannot clean that."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_APNS_HTTP.get() and record.levelno == logging.DEBUG)


def protect_transport_logs() -> None:
    request_logger = logging.getLogger("httpx")
    if not any(isinstance(item, _RequestLogFilter) for item in request_logger.filters):
        request_logger.addFilter(_RequestLogFilter())
    for name in ("hpack.hpack", "hpack.table"):
        header_logger = logging.getLogger(name)
        if not any(isinstance(item, _HeaderLogFilter) for item in header_logger.filters):
            header_logger.addFilter(_HeaderLogFilter())


def valid_device_token(value: str) -> bool:
    """Apple explicitly says not to assume a fixed token length."""
    return (
        isinstance(value, str)
        and 2 <= len(value) <= 1024
        and len(value) % 2 == 0
        and re.fullmatch(r"[0-9a-fA-F]+", value) is not None
    )


@dataclass(frozen=True)
class ApnsResponse:
    status: int
    reason: str | None = None
    retry_after: float | None = None

    @property
    def delivered(self) -> bool:
        return 200 <= self.status < 300

    @property
    def permanent_failure(self) -> bool:
        return self.status == 410 or (self.reason is not None and self.reason in PERMANENT_REASONS)


class ApnsSender(Protocol):
    async def __call__(self, url: str, headers: dict[str, str], payload: bytes) -> ApnsResponse: ...


class ApnsProvider:
    def __init__(
        self,
        team_id: str,
        key_id: str,
        topic: str,
        private_key_pem: bytes,
        *,
        environment: str = "production",
        sender: ApnsSender | None = None,
    ) -> None:
        if (
            not re.fullmatch(r"[A-Z0-9]{10}", team_id)
            or not re.fullmatch(r"[A-Z0-9]{10}", key_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}", topic)
            or environment not in HOSTS
        ):
            raise ValueError("invalid APNs provider configuration")
        try:
            key = serialization.load_pem_private_key(private_key_pem, password=None)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid APNs signing key") from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("APNs signing key must use P-256")
        self.team_id = team_id
        self.key_id = key_id
        self.topic = topic
        self.environment = environment
        self._key = key
        self._sender = sender or self._http_send
        self._jwt = ""
        self._issued_at = 0
        self._client: Any = None
        protect_transport_logs()

    def _provider_token(self) -> str:
        now = int(time.time())
        if self._jwt and 0 <= now - self._issued_at < TOKEN_TTL_SECONDS:
            return self._jwt

        def encode(value: dict[str, Any]) -> str:
            raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        unsigned = (
            encode({"alg": "ES256", "kid": self.key_id})
            + "."
            + encode({"iss": self.team_id, "iat": now})
        )
        der = self._key.sign(unsigned.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = (
            base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
            .rstrip(b"=")
            .decode("ascii")
        )
        self._jwt, self._issued_at = unsigned + "." + signature, now
        return self._jwt

    async def send(
        self,
        device_token: str,
        payload: bytes,
        *,
        environment: str | None = None,
        priority: int = PRIORITY_IMMEDIATE,
    ) -> ApnsResponse:
        target = environment or self.environment
        if not valid_device_token(device_token) or target not in HOSTS:
            raise ValueError("invalid APNs destination")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("APNs payload is too large")
        notification_id = str(uuid.uuid4())
        headers = {
            "authorization": "bearer " + self._provider_token(),
            "apns-topic": self.topic,
            "apns-push-type": "alert",
            "apns-priority": str(priority),
            "apns-id": notification_id,
            "apns-collapse-id": notification_id,
            "apns-expiration": str(int(time.time()) + 3600),
            "content-type": "application/json",
        }
        url = HOSTS[target] + "/3/device/" + device_token.lower()
        return await self._sender(url, headers, payload)

    async def _http_send(self, url: str, headers: dict[str, str], payload: bytes) -> ApnsResponse:
        import httpx

        token = _APNS_HTTP.set(True)
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    http2=True,
                    http1=False,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(10),
                    limits=httpx.Limits(max_connections=2),
                )
            try:
                response = await self._client.post(url, headers=headers, content=payload)
            except httpx.HTTPError:
                return ApnsResponse(0)
            reason: str | None = None
            if response.content:
                try:
                    body = json.loads(response.content)
                except (ValueError, UnicodeDecodeError):
                    body = {}
                candidate = body.get("reason") if isinstance(body, dict) else None
                reason = candidate if isinstance(candidate, str) and candidate in REASONS else None
            retry_after: float | None = None
            raw_retry = response.headers.get("retry-after")
            if raw_retry:
                try:
                    value = float(raw_retry)
                    retry_after = value if math.isfinite(value) and value >= 0 else None
                except ValueError:
                    retry_after = None
            return ApnsResponse(response.status_code, reason, retry_after)
        finally:
            _APNS_HTTP.reset(token)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
