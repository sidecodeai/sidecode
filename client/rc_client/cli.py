"""`rc-client` command line: enroll, run, status, agents, service, uninstall.

Exit codes are stable so the installer and other scripts can branch on them:
0 success, 1 runtime failure, 2 usage error, 3 refused with nothing done — the
device is not enrolled, or `self-update` was served a wheel that is not the
build it was asked for.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
from typing import Any

import httpx

from . import __version__, linkstate, pairing, qr
from . import config as config_module
from .agents.codex.daemon import setup as codex_setup
from .agents.grok import setup as grok_setup
from .agents.pi import commands as pi_commands
from .agents.registry import detect_all
from .build import read_build
from .channel import commands as shim_commands
from .channel.bridge import main as channel_main
from .channel.hook import EVENTS as hook_events
from .channel.hook import main as hook_main
from .config import config_exists, config_path, load_config, normalise_origin
from .daemon import Daemon
from .enroll import enroll
from .errors import RcError
from .logging_setup import setup_logging
from .proxy import DIRECT, httpx_options, redact_proxy, resolve_proxy
from .service import codex as codex_supervision
from .service import manager
from .update import self_update

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rc-client", description="SideCode device daemon")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--log-level", default=None, help="debug, info, warning or error")
    sub = parser.add_subparsers(dest="command", required=True)

    enroll_parser = sub.add_parser("enroll", help="pair this device with a gateway")
    enroll_parser.add_argument(
        "--gateway", required=True, help="gateway origin, e.g. https://rc.example.com"
    )
    source = enroll_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pair", default=None, help="pairing code RC-XXXX-XXXX minted in an app")
    source.add_argument(
        "--scan", action="store_true", help="print a QR code and wait for an app to scan it"
    )
    enroll_parser.add_argument("--name", default=None, help="device name shown in the apps")
    enroll_parser.add_argument(
        "--proxy",
        default=DIRECT,
        help=(
            "reach the gateway through an http or https proxy: `env` takes the one this "
            "machine's proxy settings name right now, a URL names one; the default dials directly"
        ),
    )

    sub.add_parser("run", help="run the daemon in the foreground")

    update_parser = sub.add_parser(
        "self-update", help="install the client build the gateway serves and restart"
    )
    update_parser.add_argument("--build", required=True, help="SHA-256 of the wheel to install")
    sub.add_parser(
        "channel", help="run the Claude Code channel bridge (started by the CLI, not by hand)"
    )
    hook_parser = sub.add_parser(
        "hook", help="run a Claude Code hook (started by Claude Code, not by hand)"
    )
    hook_parser.add_argument("event", choices=list(hook_events))

    shim_parser = sub.add_parser("shim", help="manage the claude shim used to attach sessions")
    shim_parser.add_argument("action", choices=["install", "remove", "status"])
    shim_parser.add_argument(
        "--no-shell-rc", action="store_true", help="do not touch the shell startup file"
    )
    codex_parser = sub.add_parser(
        "codex", help="manage the shared Codex app-server daemon this device attaches to"
    )
    codex_parser.add_argument("action", choices=["setup", "status"])
    codex_parser.add_argument(
        "--no-install", action="store_true", help="never run the official Codex installer"
    )
    grok_parser = sub.add_parser(
        "grok", help="manage the Grok leader this device joins terminal sessions through"
    )
    grok_parser.add_argument("action", choices=["setup", "status"])
    pi_parser = sub.add_parser(
        "pi", help="manage the pi extension that lets the apps drive pi sessions"
    )
    pi_parser.add_argument("action", choices=["setup", "status", "remove"])

    sub.add_parser("status", help="print configuration and service status")
    sub.add_parser("agents", help="print detected agents as JSON")

    service_parser = sub.add_parser("service", help="manage the background service")
    service_parser.add_argument(
        "action", choices=["install", "uninstall", "start", "restart", "stop", "status"]
    )

    uninstall_parser = sub.add_parser("uninstall", help="remove the service and its data")
    uninstall_parser.add_argument(
        "--no-shell-rc", action="store_true", help="leave the shell startup file alone"
    )
    uninstall_parser.add_argument(
        "--purge", action="store_true", help="also delete config, state and logs"
    )
    return parser


async def _claim_by_scanning(origin: str, proxy: str) -> str:
    """Print a claim URL as a QR code and wait for an app to scan it (A23)."""
    async with httpx.AsyncClient(timeout=pairing.POLL_TIMEOUT, **httpx_options(proxy)) as client:
        request = await pairing.create_request(origin, client)
        print(qr.render(request.claim_url))
        print(request.claim_url)
        print("Scan the code with the SideCode app, or open the link in a signed-in browser.")
        print("Waiting to be claimed ...")
        return await pairing.await_code(origin, request.token, client)


async def _cmd_enroll(args: argparse.Namespace) -> int:
    origin = normalise_origin(args.gateway)
    # `--proxy env` is this machine's answer, read now: the daemon's own
    # environment comes from a plist or a unit and would name nothing.
    proxy = resolve_proxy(args.proxy, origin)
    code = await _claim_by_scanning(origin, proxy) if args.scan else str(args.pair)
    agents = await detect_all()
    config = await enroll(origin, code, args.name, agents, proxy=proxy)
    available = [info.agent for info in agents if info.available]
    print(f"Enrolled as {config.device_id} at {config.gateway_origin}")
    print(f"Configuration written to {config_path()}")
    print(f"proxy: {redact_proxy(config.proxy)}")
    print("Agents detected: " + (", ".join(available) if available else "none"))
    return EXIT_OK


async def _cmd_self_update(args: argparse.Namespace) -> int:
    return EXIT_OK if await self_update(args.build) else EXIT_REFUSED


async def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    daemon = Daemon(config)
    with contextlib.suppress(asyncio.CancelledError):
        await daemon.run()
    return EXIT_OK


async def _cmd_agents(args: argparse.Namespace) -> int:
    agents = await detect_all()
    print(json.dumps([info.to_dict() for info in agents], indent=2, ensure_ascii=False))
    return EXIT_OK


async def _cmd_codex(args: argparse.Namespace) -> int:
    if args.action == "setup":
        ok, lines = await codex_setup.setup(install_missing=not args.no_install)
    else:
        ok, lines = True, (await codex_setup.status()).lines()
    for line in lines:
        print(line)
    return EXIT_OK if ok else EXIT_FAILURE


async def _cmd_grok(args: argparse.Namespace) -> int:
    if args.action == "setup":
        ok, lines = await grok_setup.setup()
    else:
        ok, lines = True, (await grok_setup.status()).lines()
    for line in lines:
        print(line)
    return EXIT_OK if ok else EXIT_FAILURE


def _cmd_pi(args: argparse.Namespace) -> int:
    if args.action == "setup":
        lines = pi_commands.setup()
    elif args.action == "remove":
        lines = pi_commands.remove()
    else:
        lines = pi_commands.status()
    for line in lines:
        print(line)
    return EXIT_OK


async def _cmd_status(args: argparse.Namespace) -> int:
    if not config_exists():
        print(f"not enrolled (no {config_path()})")
        print("run: rc-client enroll --gateway URL --pair RC-XXXX-XXXX")
        return EXIT_REFUSED
    config = load_config()
    build = read_build()
    print(f"device_id      {config.device_id}")
    print(f"name           {config.name}")
    print(f"gateway        {config.gateway_origin}")
    print(f"proxy          {redact_proxy(config.proxy)}")
    print(f"client build   {build or 'unknown (installed from source)'}")
    print(f"config         {config_path()}")
    print(f"state          {config_module.database_path()}")
    print(f"service        {manager.status()}")
    link = linkstate.read()
    if link is not None:
        print(f"gateway link   {link.summary()}")
    print(f"codex daemon   {(await codex_setup.status()).summary()}")
    print(f"grok leader    {(await grok_setup.status(probe=False)).summary()}")
    print(f"pi extension   {pi_commands.summary()}")
    return EXIT_OK


def _cmd_service(args: argparse.Namespace) -> int:
    action = args.action
    if action == "install":
        path = manager.install()
        print(f"service installed at {path}")
        hint = manager.post_install_hint()
        if hint:
            print(hint)
    elif action == "uninstall":
        manager.uninstall()
        print("service removed")
    elif action == "start":
        manager.start()
        print("service started")
    elif action == "restart":
        manager.restart()
        print("service restart requested")
    elif action == "stop":
        manager.stop()
        print("service stopped")
    else:
        print(manager.status())
    return EXIT_OK


def _cmd_shim(args: argparse.Namespace) -> int:
    shell_rc = not args.no_shell_rc
    if args.action == "install":
        lines = shim_commands.install(shell_rc=shell_rc)
    elif args.action == "remove":
        lines = shim_commands.remove(shell_rc=shell_rc)
    else:
        lines = shim_commands.status()
    for line in lines:
        print(line)
    return EXIT_OK


def _cmd_uninstall(args: argparse.Namespace) -> int:
    try:
        manager.uninstall()
    except RcError as exc:
        print(f"warning: {exc.message}", file=sys.stderr)
    print("service removed")
    codex_supervision.uninstall()
    print("codex daemon supervision removed (Codex itself is left alone)")
    for line in pi_commands.remove():
        print(line)
    for line in shim_commands.remove(shell_rc=not args.no_shell_rc):
        print(line)
    if args.purge:
        home = config_module.client_home()
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)
        print(f"removed {home}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The channel bridge owns stdout and the hook writes into a live session:
    # both have to stay quiet.
    quiet = args.command in {"channel", "hook"}
    setup_logging(args.log_level or ("warning" if quiet else None))
    handlers: dict[str, Any] = {
        "enroll": _cmd_enroll,
        "run": _cmd_run,
        "agents": _cmd_agents,
        "codex": _cmd_codex,
        "grok": _cmd_grok,
        "status": _cmd_status,
        "self-update": _cmd_self_update,
    }
    try:
        if args.command in handlers:
            return int(asyncio.run(handlers[args.command](args)))
        if args.command == "service":
            return _cmd_service(args)
        if args.command == "shim":
            return _cmd_shim(args)
        if args.command == "pi":
            return _cmd_pi(args)
        if args.command == "channel":
            return int(channel_main())
        if args.command == "hook":
            return int(hook_main(args.event))
        if args.command == "uninstall":
            return _cmd_uninstall(args)
    except RcError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.code == "not_found" and args.command != "enroll":
            return EXIT_REFUSED
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_OK
    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
