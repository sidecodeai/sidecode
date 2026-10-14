"""Security headers on HTTP responses."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket

from rc_gateway.app import build_state, create_app
from rc_gateway.headers import CONTENT_SECURITY_POLICY, PERMISSIONS_POLICY, SecurityHeaders

from .conftest import make_config

HTTPS_ORIGIN = "https://rc.example.test"


def _gateway(tmp_path: Path, **overrides: object) -> TestClient:
    return TestClient(create_app(build_state(make_config(tmp_path, **overrides))))


def _probe(*, hsts: bool) -> TestClient:
    """A minimal app behind the middleware, for the cases the gateway's routes cannot show."""
    app = FastAPI()

    @app.get("/plain")
    async def plain() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/opinionated")
    async def opinionated() -> PlainTextResponse:
        return PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    @app.websocket("/ws/probe")
    async def probe(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("ok")
        await ws.close()

    app.add_middleware(SecurityHeaders, hsts=hsts)
    return TestClient(app)


def test_http_responses_carry_the_security_headers(tmp_path: Path) -> None:
    with _gateway(tmp_path) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["permissions-policy"] == PERMISSIONS_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_error_responses_carry_them_too(tmp_path: Path) -> None:
    with _gateway(tmp_path) as client:
        response = client.get("/api/sessions")
    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/", "/index.html", "/sw.js", "/manifest.webmanifest"])
def test_the_app_shell_is_never_cached(tmp_path: Path, path: str) -> None:
    dist = tmp_path / "web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('install', () => {});", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text('{"name":"rc"}', encoding="utf-8")
    with _gateway(tmp_path) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, must-revalidate"


def test_hsts_is_absent_on_a_plain_http_origin(tmp_path: Path) -> None:
    with _gateway(tmp_path) as client:
        response = client.get("/api/health")
    assert "strict-transport-security" not in response.headers


def test_hsts_is_present_on_an_https_origin(tmp_path: Path) -> None:
    with _gateway(tmp_path, public_origin=HTTPS_ORIGIN) as client:
        response = client.get("/api/health")
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_a_header_the_route_set_is_not_overridden() -> None:
    with _probe(hsts=True) as client:
        response = client.get("/opinionated")
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_a_websocket_handshake_is_untouched() -> None:
    with _probe(hsts=True) as client, client.websocket_connect("/ws/probe") as ws:
        assert ws.receive_text() == "ok"
