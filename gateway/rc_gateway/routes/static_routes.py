"""Static delivery: the client installer, the client wheel and the web app.

These routes are registered last so ``/api/*`` and ``/ws/*`` always win. The SPA fallback returns
``index.html`` for any unknown path so client-side routing survives a reload, and a missing build
yields a plain placeholder rather than a crash, because the API has to stay usable while the web
agent is still building.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from ..client_dist import WHEEL_ALIAS, newest_wheel
from ..logging import logger
from ..security import state_of

log = logger("rc_gateway.static")
router = APIRouter()

GATEWAY_ORIGIN_PLACEHOLDER = "__GATEWAY_ORIGIN__"
IMMUTABLE = "public, max-age=31536000, immutable"
NO_STORE = "no-store, must-revalidate"
_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]{1,128}")

PLACEHOLDER_PAGE = """<!doctype html>
<title>SideCode</title>
<style>
 body { font: 15px -apple-system, system-ui, sans-serif; color: #111; background: #F5F5F4;
        margin: 0; display: grid; place-items: center; height: 100vh; }
 main { background: #fff; border: 1px solid #E6E5E1; border-radius: 16px; padding: 32px 40px;
        max-width: 520px; box-shadow: 0 1px 2px rgba(0,0,0,.06); }
 h1 { font-size: 17px; margin: 0 0 8px; }
 p { color: #6B6B6B; margin: 0 0 8px; line-height: 1.6; }
 code { font-family: ui-monospace, SF Mono, Menlo, monospace; font-size: 13px; }
</style>
<main>
  <h1>Web UI not built</h1>
  <p>The gateway is running and its API is available, but no web build was found.</p>
  <p>Build it with <code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code>, or set
     <code>WEB_DIST_DIR</code> to an existing build.</p>
</main>
"""


@router.get("/install.sh")
async def install_script(request: Request) -> Response:
    state = state_of(request)
    path = state.config.client_install_script
    if not path.is_file():
        log.warning("install script missing", path=str(path))
        return PlainTextResponse(
            "The client install script is not bundled with this gateway build.\n",
            status_code=404,
        )
    body = path.read_text(encoding="utf-8").replace(
        GATEWAY_ORIGIN_PLACEHOLDER, state.config.public_origin
    )
    return PlainTextResponse(
        body, media_type="text/x-shellscript", headers={"Cache-Control": NO_STORE}
    )


@router.get("/dist/{filename}")
async def client_wheel(filename: str, request: Request) -> Response:
    state = state_of(request)
    if _SAFE_NAME.fullmatch(filename) is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    directory = state.config.client_dist_dir
    target = newest_wheel(directory) if filename == WHEEL_ALIAS else directory / filename
    if target is None or not target.is_file() or target.parent != directory:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return FileResponse(
        target,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": NO_STORE if filename == WHEEL_ALIAS else IMMUTABLE,
            # `pip install <url>` parses the wheel's tags out of the filename, and the stable
            # alias has none, so name the real file for callers that save the response.
            "Content-Disposition": f'attachment; filename="{target.name}"',
        },
    )


@router.get("/{path:path}")
async def web_app(path: str, request: Request) -> Response:
    state = state_of(request)
    if path.startswith(("api/", "ws/")):
        # A mistyped or retired endpoint must read as an error, not as the app shell with a 200.
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    root = state.config.web_dist_dir
    index = root / "index.html"
    if not index.is_file():
        return HTMLResponse(PLACEHOLDER_PAGE, status_code=200, headers={"Cache-Control": NO_STORE})
    candidate = _resolve(root, path)
    if candidate is not None:
        cache = IMMUTABLE if _fingerprinted(candidate, root) else NO_STORE
        return FileResponse(candidate, headers={"Cache-Control": cache})
    return FileResponse(index, headers={"Cache-Control": NO_STORE})


def _resolve(root: Path, path: str) -> Path | None:
    """Map a URL path to a file inside the build, refusing anything that escapes it."""
    if not path or path.endswith("/"):
        return None
    try:
        candidate = (root / path).resolve()
        candidate.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


def _fingerprinted(candidate: Path, root: Path) -> bool:
    """Vite writes content-hashed names under ``assets/``; those are safe to cache forever."""
    try:
        return candidate.relative_to(root.resolve()).parts[0] == "assets"
    except ValueError:
        return False
