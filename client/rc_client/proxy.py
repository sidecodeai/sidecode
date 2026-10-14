"""How this device reaches its gateway: directly, or through one proxy it was told about.

The gateway link, enrollment, pairing and self-update all dial the same origin, and they dial it
directly unless the person says otherwise. Following the environment at dial time would route a
private tunnel through whatever `HTTPS_PROXY` happens to hold, and the daemon's environment is not
the one the person enrolled from anyway: it comes from a launchd plist or a systemd unit. So a host
whose only way out is a proxy, a cluster login node behind an HTTP proxy being the usual case, opts
in once, at enrollment (`rc-client enroll --proxy env`, or a URL), and `config.toml` keeps the
answer as `proxy`: `""` for a direct dial, or one proxy URL. `env` is resolved here, on the
enrolling machine, and is never stored.

Only `http` and `https` proxies are supported, and any other scheme is refused here, at enrollment:
dialling one would need a package this client does not declare, so httpx and websockets would both
raise `ImportError` on every attempt instead of connecting (docs/CLIENT.md, "Network path").
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from .errors import RcError

DIRECT = ""
ENVIRONMENT = "env"
SCHEMES = ("http", "https")


def normalise_proxy(value: str | None) -> str:
    """What `config.toml` may hold: `""` for a direct dial, or one http/https proxy URL."""
    text = (value or "").strip()
    if text == DIRECT:
        return DIRECT
    split = urlsplit(text)
    if split.scheme not in SCHEMES or not split.hostname:
        raise RcError(
            "bad_request",
            f"invalid proxy {redact_proxy(text)!r}: only http and https proxies are supported, "
            "for example http://proxy.example:3128",
        )
    return text


def redact_proxy(proxy: str) -> str:
    """A proxy as it may be printed: `direct`, or the URL without its credentials.

    A proxy URL can carry `user:password@`, and every printed form - a message, a
    log line, `rc-client status` - ends up somewhere a password must not.
    """
    text = (proxy or "").strip()
    if not text:
        return "direct"
    split = urlsplit(text if "//" in text else f"//{text}")
    host = split.netloc.rpartition("@")[2]
    return f"{split.scheme}://{host}" if split.scheme else host


def resolve_proxy(value: str | None, gateway: str) -> str:
    """The setting to store for a `--proxy` argument, with `env` resolved against this host."""
    text = (value or "").strip()
    if text.casefold() == ENVIRONMENT:
        return environment_proxy(gateway)
    return normalise_proxy(text)


def environment_proxy(gateway: str) -> str:
    """The proxy this machine's environment names for `gateway`, or `""` when it names none.

    Read once, at enrollment, by the standard library's own rules: the `https`
    entry for an `https://` gateway and the `http` entry for a plain-http one,
    nothing at all when `NO_PROXY` bypasses the gateway's host, and on macOS the
    system network settings when no variable is set. Resolving it any later would
    ask the wrong environment, because the daemon runs from a launchd plist or a
    systemd unit that carries neither variable.
    """
    split = urlsplit(gateway)
    host = split.hostname
    if not host or proxy_bypass(host):
        return DIRECT
    return normalise_proxy(getproxies().get("https" if split.scheme == "https" else "http", ""))


def httpx_options(proxy: str) -> dict[str, Any]:
    """Keyword arguments for `httpx.AsyncClient`: the stored proxy, never the environment."""
    return {"trust_env": False, "proxy": proxy or None}


def websocket_proxy(proxy: str) -> str | None:
    """The `proxy` argument for `websockets.connect`: `None` dials directly."""
    return proxy or None
