"""Configuration loading, the heartbeat, connection bounds and the origin helpers."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from rc_gateway.config import DEFAULT_TRUSTED_PROXIES, ConfigError, load_config
from rc_gateway.connections import AppConnection, Connection, SlowClientError
from rc_gateway.frames import PING_INTERVAL_SECONDS, SILENT_TIMEOUT_SECONDS
from rc_gateway.hub import Hub
from rc_gateway.index import SessionIndex
from rc_gateway.logging import redact_text, redact_value
from rc_gateway.origins import canonical_origin, origin_matches, websocket_url
from rc_gateway.security import client_ip
from rc_gateway.state import GatewayState

from .conftest import session_summary


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send_text(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def _connection(**kwargs: int) -> Connection:
    return Connection(_FakeSocket(), **kwargs)  # type: ignore[arg-type]


def test_canonical_origin_accepts_and_normalises() -> None:
    assert canonical_origin("https://rc.example.com") == "https://rc.example.com"
    assert canonical_origin("https://rc.example.com:443") == "https://rc.example.com"
    assert canonical_origin("http://rc.example.com:8787") == "http://rc.example.com:8787"
    assert canonical_origin("HTTPS://RC.Example.COM/") == "https://rc.example.com"
    assert canonical_origin("https://[::1]:8787") == "https://[::1]:8787"


def test_canonical_origin_rejects_junk() -> None:
    for value in [
        "",
        "ftp://rc.example.com",
        "https://rc.example.com/path",
        "https://rc.example.com?x=1",
        "https://user:pw@rc.example.com",
        "https://rc.example.com\\evil",
        "https://rc.example.com%2f",
        "https://rc.example.com:0",
        "https://rc.example.com:",
    ]:
        assert canonical_origin(value) is None, value


def test_origin_matching_and_websocket_urls() -> None:
    assert origin_matches("https://rc.example.com", "https://rc.example.com")
    assert not origin_matches("https://other.example.com", "https://rc.example.com")
    assert not origin_matches(None, "https://rc.example.com")
    assert websocket_url("https://rc.example.com", "/ws/app") == "wss://rc.example.com/ws/app"
    assert websocket_url("http://localhost:8787", "/ws/device") == "ws://localhost:8787/ws/device"


def test_config_requires_origin_and_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("PUBLIC_ORIGIN", "RC_PASSWORD", "RC_SECRET", "WEB_PUSH_CONTACT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(ConfigError) as missing_origin:
        load_config(load_env_file=False)
    assert "PUBLIC_ORIGIN" in str(missing_origin.value)

    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    with pytest.raises(ConfigError) as missing_password:
        load_config(load_env_file=False)
    assert "RC_PASSWORD" in str(missing_password.value)

    monkeypatch.setenv("PUBLIC_ORIGIN", "not an origin")
    monkeypatch.setenv("RC_PASSWORD", "hunter2")
    with pytest.raises(ConfigError) as bad_origin:
        load_config(load_env_file=False)
    assert "not a valid origin" in str(bad_origin.value)


def test_generated_secrets_are_stable_across_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.setenv("WEB_PUSH_CONTACT", "mailto:admin@example.com")
    monkeypatch.delenv("RC_SECRET", raising=False)

    first = load_config(load_env_file=False)
    second = load_config(load_env_file=False)
    assert first.secret == second.secret
    assert len(first.secret) >= 32
    assert first.vapid_public_key == second.vapid_public_key
    assert first.vapid_private_pem.exists()
    assert (tmp_path / "session_secret").stat().st_mode & 0o777 == 0o600
    assert first.web_push_enabled is True
    assert first.secure_cookie is True


def test_a_plain_http_origin_disables_the_secure_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "http://192.168.1.10:8787")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.delenv("WEB_PUSH_CONTACT", raising=False)
    config = load_config(load_env_file=False)
    assert config.secure_cookie is False
    assert config.web_push_enabled is False


@pytest.mark.asyncio
async def test_a_full_queue_raises_instead_of_shedding_events() -> None:
    connection = _connection(cap=2, byte_cap=10_000_000)
    await connection.send({"type": "a"})
    await connection.send({"type": "b"})
    with pytest.raises(SlowClientError):
        await connection.send({"type": "c"})


@pytest.mark.asyncio
async def test_the_byte_cap_is_enforced_independently() -> None:
    connection = _connection(cap=1000, byte_cap=2000)
    await connection.send({"type": "a", "payload": "x" * 1500})
    with pytest.raises(SlowClientError):
        await connection.send({"type": "b", "payload": "y" * 1500})
    assert connection.queued_bytes > 0


@pytest.mark.asyncio
async def test_pong_measures_latency() -> None:
    connection = _connection()
    connection.note_ping_sent()
    connection.note_pong()
    assert connection.latency_ms is not None
    assert connection.latency_ms >= 0


@pytest.mark.asyncio
async def test_heartbeat_pings_and_closes_silent_connections(
    tmp_path: Path, state: GatewayState
) -> None:
    hub = Hub(SessionIndex(tmp_path / "idx.sqlite3"), state.devices)
    socket = _FakeSocket()
    connection = AppConnection(socket, "admin", "j" * 20)  # type: ignore[arg-type]
    connection.start()
    await hub.attach_app(connection)

    await hub.heartbeat_tick()
    assert connection.queue.qsize() == 0

    connection.last_ping_at = time.monotonic() - PING_INTERVAL_SECONDS - 1
    await hub.heartbeat_tick()
    queued = connection.queue.get_nowait()
    assert '"ping"' in queued.raw

    connection.last_frame_at = time.monotonic() - SILENT_TIMEOUT_SECONDS - 1
    await hub.heartbeat_tick()
    assert socket.closed is not None
    assert socket.closed[0] == 1001
    await hub.stop()


@pytest.mark.asyncio
async def test_the_index_never_moves_a_cursor_backwards(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path / "index.sqlite3")
    await index.upsert(session_summary("s1", "d1", last_seq=40))
    index.record_seq("s1", 55)
    await index.upsert(session_summary("s1", "d1", last_seq=12))
    stored = await index.get("s1")
    assert stored is not None
    assert stored.last_seq == 55
    assert stored.summary["last_seq"] == 55
    # The same has to hold once the deferred write has landed, not only while it is outstanding.
    await index.close()
    persisted = await index.get("s1")
    assert persisted is not None
    assert persisted.last_seq == 55
    assert persisted.summary["last_seq"] == 55


@pytest.mark.asyncio
async def test_the_index_filters_and_removes(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path / "index.sqlite3")
    await index.upsert(session_summary("s1", "d1"))
    await index.upsert(session_summary("s2", "d2", archived=True))
    assert len(await index.list_sessions()) == 2
    assert len(await index.list_sessions(device_id="d1")) == 1
    assert len(await index.list_sessions(archived=True)) == 1
    removed = await index.remove("s1")
    assert removed is not None
    assert removed.device_id == "d1"
    assert await index.remove("s1") is None
    assert [item.session_id for item in await index.remove_for_device("d2")] == ["s2"]
    assert await index.list_sessions() == []


@pytest.mark.asyncio
async def test_the_index_ignores_summaries_without_identity(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path / "index.sqlite3")
    assert await index.upsert({"state": "idle"}) is None
    assert await index.upsert({"session_id": "s", "device_id": ""}) is None
    assert await index.list_sessions() == []


def test_trusted_proxies_are_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.delenv("WEB_PUSH_CONTACT", raising=False)

    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    assert load_config(load_env_file=False).trusted_proxy_networks == DEFAULT_TRUSTED_PROXIES

    monkeypatch.setenv("TRUSTED_PROXIES", "10.1.0.0/16, not-a-network ,192.168.5.4")
    assert load_config(load_env_file=False).trusted_proxy_networks == (
        "10.1.0.0/16",
        "192.168.5.4",
    )
    monkeypatch.setenv("TRUSTED_PROXIES", "garbage")
    assert load_config(load_env_file=False).trusted_proxy_networks == DEFAULT_TRUSTED_PROXIES


def test_forwarded_for_takes_the_last_untrusted_hop(state: GatewayState) -> None:
    """An appending proxy leaves a caller-chosen value in front; it must not pick the bucket."""
    from starlette.datastructures import Headers

    def address(peer: str, forwarded: str | None) -> str:
        raw = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/login",
            "headers": raw,
            "client": (peer, 12345),
            "app": SimpleNamespace(state=SimpleNamespace(gateway=state)),
        }
        request = Request(scope)
        assert isinstance(request.headers, Headers)
        return client_ip(request, state)

    # Direct callers are their own address, header or not.
    assert address("203.0.113.9", "1.2.3.4") == "203.0.113.9"
    # Behind the trusted proxy, the last hop the proxy itself did not add is the caller.
    assert address("127.0.0.1", "198.51.100.7") == "198.51.100.7"
    assert address("127.0.0.1", "1.2.3.4, 198.51.100.7") == "198.51.100.7"
    assert address("127.0.0.1", None) == "127.0.0.1"
    # A private peer is no longer trusted by default.
    assert address("10.1.2.3", "198.51.100.7") == "10.1.2.3"


def test_secrets_are_never_written_world_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.setenv("WEB_PUSH_CONTACT", "mailto:admin@example.com")
    monkeypatch.delenv("RC_SECRET", raising=False)
    config = load_config(load_env_file=False)
    assert config.data_dir.stat().st_mode & 0o777 == 0o700
    for name in ("session_secret", "vapid_private.pem"):
        assert (config.data_dir / name).stat().st_mode & 0o777 == 0o600


def test_every_database_is_owner_only(tmp_path: Path) -> None:
    from rc_gateway.app import build_state

    from .conftest import make_config

    built = build_state(make_config(tmp_path))
    for store in (built.devices, built.index, built.push_store):
        assert store.path.stat().st_mode & 0o777 == 0o600, store.path.name


def test_log_redaction_removes_credentials() -> None:
    assert "hunter2" not in redact_text('{"password": "hunter2"}')
    assert "abc123" not in redact_text("Authorization: Bearer abc123")
    assert "RC-7K42-QX9M" not in redact_text("pairing code RC-7K42-QX9M issued")
    assert "?token=1" not in redact_text("GET /ws/app?token=1 HTTP/1.1")
    payload: dict[str, Any] = {"device_token": "secret", "kind": "status"}
    redacted = redact_value(payload)
    assert redacted == {"device_token": "[redacted]", "kind": "status"}


def test_log_redaction_hides_passwords_and_email_addresses() -> None:
    assert redact_text("invite sent to zoe.private@example.com") == (
        "invite sent to [redacted]@example.com"
    )
    fields = {"email": "zoe@example.com", "current_password": "a", "new_password": "b", "kind": "u"}
    assert redact_value(fields) == {
        "email": "[redacted]",
        "current_password": "[redacted]",
        "new_password": "[redacted]",
        "kind": "u",
    }


@pytest.mark.asyncio
async def test_the_index_refuses_a_summary_from_a_foreign_device(tmp_path: Path) -> None:
    """The session-to-device binding is set once and never rebound by another announcement."""
    index = SessionIndex(tmp_path / "index.sqlite3")
    assert await index.upsert(session_summary("s1", "device-a")) is not None
    assert await index.owner("s1") == "device-a"
    assert await index.owner("missing") is None

    hijack = await index.upsert(session_summary("s1", "device-b", title="stolen"))
    assert hijack is None
    stored = await index.get("s1")
    assert stored is not None
    assert stored.device_id == "device-a"
    assert stored.summary["title"] != "stolen"

    # The real owner can still update it.
    updated = await index.upsert(session_summary("s1", "device-a", title="fine"))
    assert updated is not None and updated.summary["title"] == "fine"


@pytest.mark.asyncio
async def test_replay_buffers_are_bounded_and_least_recently_used_wins(
    tmp_path: Path, state: GatewayState
) -> None:
    """A device streaming many sessions must not grow gateway memory without bound."""
    from rc_gateway.hub import MAX_REPLAY_BUFFERS, Hub

    hub = Hub(SessionIndex(tmp_path / "buffers.sqlite3"), state.devices)
    for index in range(MAX_REPLAY_BUFFERS + 20):
        hub._buffer_for(f"session-{index}")
    assert len(hub._buffers) == MAX_REPLAY_BUFFERS
    # The coldest were evicted; the newest survive.
    assert "session-0" not in hub._buffers
    assert f"session-{MAX_REPLAY_BUFFERS + 19}" in hub._buffers

    # Touching a buffer makes it the most recent, so it outlives a later wave of new ones.
    warm = f"session-{MAX_REPLAY_BUFFERS + 5}"
    hub._buffer_for(warm)
    for index in range(MAX_REPLAY_BUFFERS - 1):
        hub._buffer_for(f"late-{index}")
    assert warm in hub._buffers
    await hub.stop()


def test_the_replay_aggregate_fits_the_container() -> None:
    """GW-6: the stated bound is a product, and it has to fit `docker-compose.yml`'s memory."""
    from rc_gateway.budget import MAX_INFLIGHT_BYTES
    from rc_gateway.hub import MAX_REPLAY_BUFFERS
    from rc_gateway.replay import MAX_BYTES

    mem_limit = 512 * 1024 * 1024
    stated = MAX_REPLAY_BUFFERS * MAX_BYTES + MAX_INFLIGHT_BYTES
    assert stated <= mem_limit // 2


@pytest.mark.asyncio
async def test_queue_snapshots_and_session_owners_are_bounded(
    tmp_path: Path, state: GatewayState
) -> None:
    """GW-6: both maps are keyed by a session id a device chose, so neither may grow freely."""
    from rc_gateway.hub import MAX_TRACKED_QUEUES, MAX_TRACKED_SESSION_OWNERS, Hub

    hub = Hub(SessionIndex(tmp_path / "maps.sqlite3"), state.devices)
    for index in range(MAX_TRACKED_QUEUES + 10):
        hub._remember_queue(f"queue-{index}", {"seq": index, "kind": "queue"})
    assert len(hub._queues) == MAX_TRACKED_QUEUES
    assert "queue-0" not in hub._queues

    for index in range(MAX_TRACKED_SESSION_OWNERS + 10):
        hub._remember_session_owner(f"owned-{index}", "device-a")
    assert len(hub._owners) == MAX_TRACKED_SESSION_OWNERS
    assert "owned-0" not in hub._owners
    await hub.stop()
