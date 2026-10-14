"""Installer, client wheel and web app delivery."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.client_dist import newest_wheel, served_client
from rc_gateway.routes.static_routes import _resolve

from .conftest import ORIGIN, make_config


def _app(tmp_path: Path) -> TestClient:
    return TestClient(create_app(build_state(make_config(tmp_path))))


def test_install_script_substitutes_the_origin(tmp_path: Path) -> None:
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/sh\nGATEWAY=__GATEWAY_ORIGIN__\n", encoding="utf-8")
    with _app(tmp_path) as client:
        response = client.get("/install.sh")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert response.text == f"#!/bin/sh\nGATEWAY={ORIGIN}\n"
    assert "__GATEWAY_ORIGIN__" not in response.text


def test_a_missing_install_script_says_so(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        response = client.get("/install.sh")
    assert response.status_code == 404
    assert "install script" in response.text


def test_the_wheel_alias_resolves_to_the_newest_build(tmp_path: Path) -> None:
    dist = tmp_path / "client-dist"
    dist.mkdir()
    (dist / "rc_client-0.1.0-py3-none-any.whl").write_bytes(b"old")
    (dist / "rc_client-0.2.0-py3-none-any.whl").write_bytes(b"new")
    os.utime(dist / "rc_client-0.1.0-py3-none-any.whl", (1, 1))
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    with _app(tmp_path) as client:
        alias = client.get("/dist/rc_client-latest.whl")
        exact = client.get("/dist/rc_client-0.1.0-py3-none-any.whl")
        missing = client.get("/dist/nothing.whl")
        escaped = client.get("/dist/%2e%2e/secret.txt")
    assert alias.status_code == 200 and alias.content == b"new"
    assert exact.status_code == 200 and exact.content == b"old"
    assert missing.status_code == 404
    assert b"top secret" not in escaped.content
    # The alias has no PEP 427 tags, so `pip install` can only use it via the real filename.
    assert alias.headers["content-disposition"] == (
        'attachment; filename="rc_client-0.2.0-py3-none-any.whl"'
    )
    assert exact.headers["content-disposition"] == (
        'attachment; filename="rc_client-0.1.0-py3-none-any.whl"'
    )


def test_the_wheel_alias_is_404_without_a_build(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        response = client.get("/dist/rc_client-latest.whl")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_served_client_is_named_by_its_version_and_hash(tmp_path: Path) -> None:
    """A22: `GET /api/config` reports exactly the wheel `/dist/rc_client-latest.whl` hands out."""
    dist = tmp_path / "client-dist"
    dist.mkdir()
    (dist / "rc_client-0.4.0-py3-none-any.whl").write_bytes(b"wheel bytes")
    served = served_client(dist)
    assert served is not None
    assert served.version == "0.4.0"
    assert served.build == hashlib.sha256(b"wheel bytes").hexdigest()
    assert served.url == "/dist/rc_client-latest.whl"
    assert served_client(tmp_path / "missing") is None


def test_wheel_filenames_are_restricted(tmp_path: Path) -> None:
    dist = tmp_path / "client-dist"
    dist.mkdir()
    (dist / "rc_client-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    assert newest_wheel(dist) is not None
    assert newest_wheel(tmp_path / "missing") is None
    assert _resolve(dist, "../secret.txt") is None
    assert _resolve(dist, "rc_client-0.1.0-py3-none-any.whl") is not None


def test_placeholder_page_when_the_web_build_is_missing(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Web UI not built" in response.text


def test_spa_fallback_and_cache_headers(tmp_path: Path) -> None:
    dist = tmp_path / "web-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('push', () => {})", encoding="utf-8")
    with _app(tmp_path) as client:
        index = client.get("/")
        deep = client.get("/sessions/abc")
        asset = client.get("/assets/index-abc123.js")
        worker = client.get("/sw.js")
    assert index.text.startswith("<!doctype html>")
    assert deep.text == index.text
    assert index.headers["cache-control"] == "no-store, must-revalidate"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert worker.headers["cache-control"] == "no-store, must-revalidate"


def test_the_spa_fallback_never_escapes_the_build(tmp_path: Path) -> None:
    dist = tmp_path / "web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    with _app(tmp_path) as client:
        escaped = client.get("/../secret.txt")
        encoded = client.get("/%2e%2e/secret.txt")
    assert b"top secret" not in escaped.content
    assert b"top secret" not in encoded.content


def test_api_routes_win_over_the_spa_fallback(tmp_path: Path) -> None:
    dist = tmp_path / "web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    with _app(tmp_path) as client:
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/api/devices").status_code == 401


def test_an_unknown_api_path_returns_the_error_envelope(tmp_path: Path) -> None:
    """A mistyped endpoint must not read as the app shell with a 200."""
    dist = tmp_path / "web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    with _app(tmp_path) as client:
        for path in ("/api/nope", "/api/devices/typo/extra", "/ws/nope"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json()["error"]["code"] == "not_found", path
        assert client.get("/sessions/abc").status_code == 200


def test_the_wheel_is_served_with_its_real_filename(tmp_path: Path) -> None:
    """`pip install <url>` reads the wheel's tags from the filename, so the alias must name it."""
    dist = tmp_path / "client-dist"
    dist.mkdir()
    (dist / "rc_client-0.3.0-py3-none-any.whl").write_bytes(b"wheel")
    with _app(tmp_path) as client:
        alias = client.get("/dist/rc_client-latest.whl")
    assert alias.status_code == 200
    assert alias.headers["content-disposition"] == (
        'attachment; filename="rc_client-0.3.0-py3-none-any.whl"'
    )
