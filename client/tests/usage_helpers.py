"""A stand-in for Anthropic's usage endpoint, and the synthetic credentials it is read with (A33).

`serve` replaces only the network under the device's own request code, with an
`httpx.MockTransport`: the client, the headers, the deadline, the parsing and the way a failure is
told all run for real. Nothing here is a person's credential, and nothing here dials out.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from rc_client.agents import anthropic_usage, usage_fetch
from rc_client.agents.claude import account as claude_account
from rc_client.agents.usage_memory import UsageMemory
from rc_client.models import now_ms

TOKEN = "test-access-token"
HOUR_MS = 3_600_000

# The usage body `GET /api/oauth/usage` answers with, emptied of anything
# private: a five-hour window, a week, and a week confined to one model.
USAGE_BODY: dict[str, Any] = {
    "five_hour": {"utilization": 16.0, "resets_at": "2026-09-15T15:40:00.000000+00:00"},
    "seven_day": {"utilization": 54.0, "resets_at": "2026-09-16T22:00:00.000000+00:00"},
    "seven_day_opus": None,
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 16,
            "severity": "normal",
            "resets_at": "2026-09-15T15:40:00.000000+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 54,
            "severity": "normal",
            "resets_at": "2026-09-16T22:00:00.000000+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 64,
            "severity": "normal",
            "resets_at": "2026-09-16T22:00:00.000000+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": True,
        },
    ],
}
# What `USAGE_BODY` comes to on the wire, window by window.
USAGE_WINDOWS = [
    {"window_minutes": 300, "used_percent": 16, "resets_at": 1789486800000},
    {"window_minutes": 10080, "used_percent": 54, "resets_at": 1789596000000},
    {"window_minutes": 10080, "scope": "Fable", "used_percent": 64, "resets_at": 1789596000000},
]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def claude_credential(
    expires_at: int, subscription: str = "max", token: str = TOKEN
) -> dict[str, Any]:
    return {
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": "test-refresh-token",
            "expiresAt": expires_at,
            "subscriptionType": subscription,
            "rateLimitTier": "default_claude_max_5x",
        }
    }


EMAIL = "me@example.com"


def sign_in_claude(token: str = TOKEN, email: str | None = EMAIL) -> None:
    """Leave Claude Code signed in, in the files `claude_account` reads, for another hour.

    The account has an email, because nothing else tells it from another account of the vendor;
    `email=None` is the account that has none.
    """
    write_json(
        claude_account.credentials_file(), claude_credential(now_ms() + HOUR_MS, token=token)
    )
    if email is not None:
        write_json(claude_account.config_file(), {"oauthAccount": {"emailAddress": email}})


@dataclass(frozen=True)
class Reply:
    """What the stand-in endpoint answers one request with: a response, or a failure to get one."""

    status: int = 200
    json: Any = None
    text: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    raises: Exception | None = None

    def response(self) -> httpx.Response:
        if self.raises is not None:
            raise self.raises
        # On the wire a header is bytes, latin-1 by the standard, and not always plain ASCII.
        headers = [(name.encode(), value.encode("latin-1")) for name, value in self.headers.items()]
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, headers=headers)
        return httpx.Response(self.status, json=self.json, headers=headers)


def ok(body: Any = USAGE_BODY) -> Reply:
    return Reply(200, json=body)


def refusal(status: int, error_type: str | None = None, **headers: str) -> Reply:
    """A non-200 answer; `retry_after="30"` is the header `retry-after: 30`."""
    body = {"error": {"type": error_type, "message": "a sentence the log must not repeat"}}
    named = {name.replace("_", "-"): value for name, value in headers.items()}
    return Reply(status, json=body if error_type else None, headers=named)


def page(text: str) -> Reply:
    """A 200 that is not JSON, such as a captive portal's page."""
    return Reply(200, text=text, headers={"content-type": "text/html"})


def failing(exc: Exception) -> Reply:
    return Reply(raises=exc)


Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def answering(*replies: Reply) -> Handler:
    """A handler that gives `replies` in turn, and the last of them to every request after."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        return (queue.pop(0) if len(queue) > 1 else queue[0]).response()

    return handler


class Served:
    """The requests the stand-in endpoint has seen, and how the client that sent them was built."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.client: dict[str, Any] = {}

    @property
    def tokens(self) -> list[str]:
        return [
            request.headers["authorization"].removeprefix("Bearer ") for request in self.requests
        ]


def serve(monkeypatch: pytest.MonkeyPatch, *replies: Reply) -> Served:
    """Put a stand-in under every `httpx.AsyncClient` the device builds from now on."""
    return serve_with(monkeypatch, answering(*replies))


def serve_with(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> Served:
    """`serve` for a handler of the test's own, which may wait before it answers."""
    served = Served()
    real_init = httpx.AsyncClient.__init__

    async def answer(request: httpx.Request) -> httpx.Response:
        served.requests.append(request)
        reply = handler(request)
        return reply if isinstance(reply, httpx.Response) else await reply

    def init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        served.client = dict(kwargs)
        # A mock transport is no proxy's business: with one set, httpx would dial it for real.
        kwargs.pop("proxy", None)
        real_init(self, transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    return served


# The protocol time `keep_time` starts at: an hour before the first reset of `USAGE_BODY`, so that
# its windows are still ahead of the clock for the half hour an earlier reading may stand in.
READ_AT = 1789483200000


def usage_body_resetting(session_minutes: int | None, week_minutes: int | None) -> dict[str, Any]:
    """A usage body whose windows reset that many minutes after `READ_AT`, or never say when."""

    def at(minutes: int | None) -> str | None:
        if minutes is None:
            return None
        return datetime.fromtimestamp((READ_AT + minutes * 60_000) / 1000, UTC).isoformat()

    return {
        "limits": [
            {"kind": "session", "percent": 40, "resets_at": at(session_minutes), "scope": None},
            {"kind": "weekly_all", "percent": 70, "resets_at": at(week_minutes), "scope": None},
        ]
    }


class Clock:
    """A clock that moves when a test says so.

    Called, it gives the seconds the device holds an answer by; `wall_ms` is the protocol time it
    stamps a read with, and both move together.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.wall_ms = READ_AT

    def __call__(self) -> float:
        return self.now

    def now_ms(self) -> int:
        return self.wall_ms

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall_ms += round(seconds * 1000)


def keep_time(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Hold the device's quota memory, and the time it stamps reads with, to a test's own clock."""
    clock = Clock()
    monkeypatch.setattr(anthropic_usage, "memory", UsageMemory(clock=clock))
    monkeypatch.setattr(anthropic_usage, "now_ms", clock.now_ms)
    monkeypatch.setattr(usage_fetch, "now_ms", clock.now_ms)
    return clock
