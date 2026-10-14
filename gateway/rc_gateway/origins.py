"""Origin canonicalisation for the CSRF surface.

Adapted from cc-remote's ``cc_remote/relay/origins.py`` (MIT, see THIRD_PARTY_NOTICES.md). The
gateway compares a request's ``Origin`` against the configured ``PUBLIC_ORIGIN`` and never against a
forwarded header, so a reverse proxy cannot be tricked into widening the check. Unlike upstream this
accepts ``http://`` as well, because a bare-VPS deployment without a domain is a supported mode.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_origin(value: str) -> str | None:
    """Return ``scheme://host[:port]`` in canonical form, or ``None`` when unusable."""
    if (
        not value
        or len(value) > 2048
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value
        or "%" in value
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    try:
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or parsed.netloc.endswith(":")
    ):
        return None
    try:
        address = ipaddress.ip_address(hostname)
        host = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        if len(host) > 253 or not all(
            _LABEL.fullmatch(label) for label in host.removesuffix(".").split(".")
        ):
            return None
    if port == 0:
        return None
    suffix = "" if port in (None, _DEFAULT_PORTS[scheme]) else f":{port}"
    return f"{scheme}://{host}{suffix}"


def origin_matches(header_value: str | None, expected: str) -> bool:
    """True when an ``Origin`` header equals the configured public origin."""
    if not header_value:
        return False
    canonical = canonical_origin(header_value)
    return canonical is not None and canonical == expected


def websocket_url(public_origin: str, path: str) -> str:
    """Derive the ``ws(s)://`` URL apps and devices should dial for ``path``."""
    scheme = "wss" if public_origin.startswith("https://") else "ws"
    authority = public_origin.split("://", 1)[1]
    return f"{scheme}://{authority}{path}"
