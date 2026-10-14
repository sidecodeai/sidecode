"""Test fixtures: a complete gateway on a temporary directory with injected senders."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.apns import ApnsProvider, ApnsResponse
from rc_gateway.app import build_state, create_app
from rc_gateway.auto_update import UPDATE_RETRY_SECONDS
from rc_gateway.badge import BadgeHook
from rc_gateway.config import ApnsConfig, Config, PolishConfig, SttConfig
from rc_gateway.connections import AppConnection, Connection, DeviceConnection
from rc_gateway.devices import DeviceStore
from rc_gateway.frames import REQUEST_TIMEOUT_SECONDS
from rc_gateway.hub import (
    OFFLINE_GRACE_SECONDS,
    UPDATE_TIMEOUT_SECONDS,
    Hub,
    ResumeHook,
    TransitionHook,
)
from rc_gateway.index import SessionIndex
from rc_gateway.polish import PolishError, PolishModel
from rc_gateway.polish_prompt import ContextMessage
from rc_gateway.push_store import WebPushSubscription
from rc_gateway.state import GatewayState
from rc_gateway.stt import SttError, Transcript

ORIGIN = "http://testserver"
PASSWORD = "correct-horse-battery-staple"
MEMBER_PASSWORD = "another-correct-horse-staple"
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"


class FakeTranscriber:
    """Returns a canned transcript and records what it was asked to transcribe.

    ``language`` is what the provider reports having detected; nothing tells it one (A44).
    """

    def __init__(self, text: str = "hello world", language: str = "en") -> None:
        self.text = text
        self.language = language
        self.calls: list[dict[str, Any]] = []
        self.fail: str | None = None

    async def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> Transcript:
        self.calls.append({"bytes": len(audio), "filename": filename, "content_type": content_type})
        if self.fail is not None:
            raise SttError(self.fail)
        return Transcript(text=self.text, language=self.language)


class FakePolisher:
    """Returns a canned polished text and records what it was asked to polish (A29)."""

    def __init__(self, text: str = "Polished words.") -> None:
        self.text = text
        self.models_offered = [PolishModel(id="fake-a", label="fake-a")]
        self.calls: list[dict[str, Any]] = []
        self.fail: str | None = None
        self.models_fail: str | None = None

    async def models(self) -> list[PolishModel]:
        if self.models_fail is not None:
            raise PolishError(self.models_fail)
        return list(self.models_offered)

    async def polish(
        self,
        text: str,
        *,
        model: str,
        strength: str,
        language: str | None,
        context: Sequence[ContextMessage],
    ) -> str:
        self.calls.append(
            {
                "text": text,
                "model": model,
                "strength": strength,
                "language": language,
                "context": list(context),
            }
        )
        if self.fail is not None:
            raise PolishError(self.fail)
        return self.text


class FakeWebPushSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.status: int | None = 201

    async def __call__(self, subscription: WebPushSubscription, payload: str) -> int | None:
        self.sent.append((subscription.endpoint, json.loads(payload)))
        return self.status


async def no_unseen(username: str) -> int:
    """The A47 count for a push test that is not about it: nothing is marked."""
    return 0


@dataclass
class ApnsRecorder:
    """Stands in for Apple: records every request and answers as told."""

    requests: list[tuple[dict[str, str], dict[str, Any]]] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)

    async def __call__(self, url: str, headers: dict[str, str], payload: bytes) -> ApnsResponse:
        self.requests.append((headers, json.loads(payload)))
        return ApnsResponse(self.statuses.pop(0) if self.statuses else 200)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [body for _, body in self.requests]


def apns_provider(recorder: ApnsRecorder) -> ApnsProvider:
    """A provider with a throwaway P-256 key that sends to ``recorder`` instead of Apple."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return ApnsProvider(
        "ABCDE12345", "FGHIJ67890", "com.example.app", pem, environment="sandbox", sender=recorder
    )


class VirtualClock:
    """``asyncio.sleep`` on a clock the test moves, so a quiet period never costs real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        """Move the clock, wake every sleeper whose time has come, and let them run.

        A task created just before the call has not reached its sleep yet, so it is given the
        loop first: the time passes after it started waiting, as it would on a real clock.
        """
        await self._run_ready()
        self.now += seconds
        # Within a nanosecond, so that steps such as 2.9 and then 0.1 add up to the 3.0 they mean.
        reached = self.now + 1e-9
        due = [future for deadline, future in self._sleepers if deadline <= reached]
        self._sleepers = [item for item in self._sleepers if item[0] > reached]
        for future in due:
            if not future.done():
                future.set_result(None)
        await self._run_ready()

    @staticmethod
    async def _run_ready() -> None:
        for _ in range(5):
            await asyncio.sleep(0)


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    values: dict[str, Any] = {
        "public_origin": ORIGIN,
        "password": PASSWORD,
        "secret": "test-secret-value-that-is-long-enough",
        "data_dir": tmp_path,
        "host": "127.0.0.1",
        "port": 8787,
        "log_level": "warning",
        "web_dist_dir": tmp_path / "web-dist",
        "client_install_script": tmp_path / "install.sh",
        "client_dist_dir": tmp_path / "client-dist",
        "web_push_contact": "mailto:admin@example.com",
        "stt": SttConfig(
            provider="openai",
            base_url="http://stt.invalid/v1",
            api_key="k",
            model="whisper-1",
        ),
        "polish": PolishConfig(
            base_url="http://polish.invalid/v1",
            api_key="k",
            models=(),
            timeout_seconds=20.0,
        ),
        "apns": ApnsConfig(team_id="", key_id="", key_path="", topic="", environment="production"),
        "vapid_private_pem": tmp_path / "vapid_private.pem",
        "vapid_public_key": "BJ" + "A" * 84,
    }
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def polisher() -> FakePolisher:
    return FakePolisher()


@pytest.fixture
def web_sender() -> FakeWebPushSender:
    return FakeWebPushSender()


@pytest.fixture
def state(
    tmp_path: Path,
    transcriber: FakeTranscriber,
    polisher: FakePolisher,
    web_sender: FakeWebPushSender,
) -> GatewayState:
    built = build_state(
        make_config(tmp_path),
        transcriber=transcriber,
        polisher=polisher,
        web_sender=web_sender,
    )
    built.hub = Hub(
        built.index,
        built.devices,
        on_session_transition=built.push.on_session_transition,
        on_session_resume=built.push.on_session_resume,
        on_badge_change=built.push.on_badge_change,
        request_timeout=0.4,
        # Short enough to drive the A13 grace period in a test, and well inside the request
        # timeout above so a parked request is still answered `device_offline` rather than
        # `timeout` when the period ends without a reconnect.
        offline_grace=0.2,
        # A36: the automatic update is live over the sockets, and a test that writes a wheel with
        # `write_wheel` gets the same behaviour a deployed gateway has.
        served_build=built.served_build,
        update_retry=0.2,
    )
    return built


@pytest.fixture
def client(state: GatewayState) -> Iterator[TestClient]:
    with TestClient(create_app(state)) as test_client:
        yield test_client


@pytest.fixture
def token(client: TestClient) -> str:
    return sign_in(client, "admin", PASSWORD)


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sign_in(client: TestClient, username: str, password: str) -> str:
    """Sign in and return the bearer token. Every account signs in the same way (A24)."""
    response = client.post(
        "/api/login",
        json={"username": username, "password": password},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    value: str = response.json()["token"]
    return value


def add_member(
    client: TestClient,
    admin: dict[str, str],
    username: str,
    password: str = MEMBER_PASSWORD,
) -> dict[str, str]:
    """Create a member account as the admin and return its bearer headers."""
    created = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": "member"},
        headers=admin,
    )
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {sign_in(client, username, password)}"}


def write_wheel(tmp_path: Path, version: str = "0.1.0", body: bytes = b"wheel") -> str:
    """Put a client wheel where the gateway serves it, and return its SHA-256 (A22)."""
    dist = tmp_path / "client-dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / f"rc_client-{version}-py3-none-any.whl").write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def enroll_device(
    client: TestClient, auth: dict[str, str], name: str = "mac-studio"
) -> dict[str, Any]:
    """Mint a pairing code and redeem it, returning the enrollment response plus the code."""
    minted = client.post("/api/devices/pairing", headers=auth)
    assert minted.status_code == 200, minted.text
    code = minted.json()["code"]
    enrolled = client.post(
        "/api/devices/enroll",
        json={
            "code": code,
            "name": name,
            "platform": "macos",
            "hostname": "studio.local",
            "arch": "arm64",
            "client_version": "0.1.0",
        },
    )
    assert enrolled.status_code == 200, enrolled.text
    payload: dict[str, Any] = enrolled.json()
    payload["code"] = code
    return payload


def device_hello(**overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "hello",
        "protocol": 1,
        "client_version": "0.1.0",
        "name": "mac-studio",
        "platform": "macos",
        "hostname": "studio.local",
        "arch": "arm64",
        "agents": [
            {
                "agent": "claude",
                "available": True,
                "version": "2.1.266",
                "path": "/usr/local/bin/claude",
                "models": [{"id": "claude-sonnet-4-5", "label": "Sonnet 4.5"}],
                "default_model": "claude-sonnet-4-5",
                "permission_modes": [{"id": "default", "label": "Ask before edits"}],
                "default_permission_mode": "default",
                "efforts": [],
                "default_effort": None,
                "capabilities": ["interrupt", "queue", "history"],
            }
        ],
        "sessions": [],
    }
    frame.update(overrides)
    return frame


def session_summary(session_id: str, device_id: str, **overrides: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "session_id": session_id,
        "device_id": device_id,
        "agent": "claude",
        "title": "Fix flaky auth test",
        "cwd": "/Users/me/dev/gateway",
        "git": None,
        "state": "idle",
        "state_detail": None,
        "origin": "remote",
        "control": "remote",
        "model": "claude-sonnet-4-5",
        "permission_mode": "default",
        "effort": None,
        "created_at": 1710000000000,
        "updated_at": 1710000000000,
        "last_seq": 0,
        "archived": False,
        "turn": None,
        "todos": None,
        "usage": None,
        "queued": 0,
    }
    summary.update(overrides)
    return summary


def close_code_for(client: TestClient, path: str, **kwargs: Any) -> int:
    """Connect and return the close code the gateway sends.

    Amendment A4 requires the handshake to complete so the code is observable, so the rejection
    shows up on the first receive rather than as a failed upgrade.
    """
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect(path, **kwargs) as socket,
    ):
        for _ in range(5):
            socket.receive_json()
    return int(caught.value.code)


def collect_until(ws: Any, kind: str, limit: int = 40) -> list[dict[str, Any]]:
    """Read frames up to and including the first of ``kind``, returning all of them."""
    seen: list[dict[str, Any]] = []
    for _ in range(limit):
        frame = dict(ws.receive_json())
        seen.append(frame)
        if frame.get("type") == kind:
            return seen
    raise AssertionError(f"{kind!r} never arrived; saw {[f.get('type') for f in seen]}")


def drain_until(ws: Any, kind: str, limit: int = 40) -> dict[str, Any]:
    """Read frames until one of ``kind`` arrives; fail loudly instead of hanging forever."""
    seen: list[str] = []
    for _ in range(limit):
        frame = ws.receive_json()
        seen.append(str(frame.get("type")))
        if frame.get("type") == kind:
            return dict(frame)
    raise AssertionError(f"{kind!r} never arrived; saw {seen}")


class FakeSocket:
    """A WebSocket stand-in for hub tests that run no server."""

    async def send_text(self, raw: str) -> None:
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


@dataclass
class HubRig:
    """A hub with a real store behind it, one enrolled device and one app."""

    hub: Hub
    index: SessionIndex
    devices: DeviceStore
    device: DeviceConnection
    app: AppConnection
    device_id: str


async def hub_rig(
    tmp_path: Path,
    *,
    on_session_transition: TransitionHook | None = None,
    on_session_resume: ResumeHook | None = None,
    on_badge_change: BadgeHook | None = None,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    offline_grace: float = OFFLINE_GRACE_SECONDS,
    update_timeout: float = UPDATE_TIMEOUT_SECONDS,
    update_retry: float = UPDATE_RETRY_SECONDS,
    served_build: Callable[[], str | None] | None = None,
) -> HubRig:
    """Build a hub whose connections never drain their queues.

    Leaving the writer tasks unstarted keeps what the gateway produced in the queues, so a test
    reads exactly the frames the hub emitted, in order, without racing a sender.
    """
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    devices = DeviceStore(tmp_path / "devices.sqlite3")
    grant = await devices.create_pairing("admin")
    enrolled = await devices.redeem(
        grant.code,
        name="mac-studio",
        platform="macos",
        hostname="studio.local",
        arch="arm64",
        client_version="0.1.0",
    )
    assert not isinstance(enrolled, str), enrolled
    hub = Hub(
        index,
        devices,
        on_session_transition=on_session_transition,
        on_session_resume=on_session_resume,
        on_badge_change=on_badge_change,
        request_timeout=request_timeout,
        offline_grace=offline_grace,
        update_timeout=update_timeout,
        update_retry=update_retry,
        served_build=served_build,
    )
    device = fake_device(enrolled.device_id)
    app = fake_app()
    await hub.attach_device(device)
    await hub.attach_app(app)
    return HubRig(
        hub=hub,
        index=index,
        devices=devices,
        device=device,
        app=app,
        device_id=enrolled.device_id,
    )


def fake_device(device_id: str) -> DeviceConnection:
    return DeviceConnection(FakeSocket(), device_id)  # type: ignore[arg-type]


def fake_app(username: str = "admin") -> AppConnection:
    return AppConnection(FakeSocket(), username)  # type: ignore[arg-type]


def frames_of(connection: Connection) -> list[dict[str, Any]]:
    """Everything queued for a connection, drained."""
    out: list[dict[str, Any]] = []
    while not connection.queue.empty():
        item = connection.queue.get_nowait()
        out.append(json.loads(item.raw))
    return out
