"""Keeps the test suite away from the agents installed on the machine it runs on.

A test that detects, resolves or probes an agent must meet a fake in a
temporary directory, never the person's own `codex`, `claude`, `grok` or `pi`:
running one costs tokens and touches their state, and a test has no business
doing either. Two layers, both wired in `conftest.py`:

- `fakes_only` wraps each runtime's `resolve_binary`, so a plain detection
  finds nothing that lives outside the temporary directories and
  `tests/fixtures`. A test that means to find a fake points `RC_<AGENT>_BIN`, or
  `PATH`, at one it wrote.
- `install` adds an audit hook that refuses to start such a program however a
  test reached it (`subprocess`, `asyncio`, `os.posix_spawn`, `os.exec*`,
  `os.spawn*`), before the child exists. It raises, and it records the attempt
  as well, because a caller that catches every exception would otherwise hide
  it: `conftest.py` fails the test at its end when an attempt was made.

A program that is not there starts nothing, so a made-up path such as
`/bin/pi` is let through. `RC_REAL_AGENTS=1` is the opt-in for the smoke tests
that do mean to drive the agents on this machine.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

OPT_IN = "RC_REAL_AGENTS"
# The agents' executables. `agent` is the name of Grok Build's own launcher.
AGENT_NAMES = frozenset({"claude", "codex", "grok", "pi", "agent"})
# The audit events that start a program, and what each one's arguments are.
SPAWNS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"})

# Where a fake agent may live: the temporary directories, and the fixtures a
# test reads. Real paths, so a symlink out of them does not count as inside.
ROOTS: list[str] = [
    os.path.realpath(root)
    for root in (tempfile.gettempdir(), "/tmp", "/var/tmp", str(Path(__file__).parent / "fixtures"))
]
# The attempts made so far in the running test, which `conftest.py` checks.
VIOLATIONS: list[str] = []


class RealAgentStarted(RuntimeError):
    """A test tried to start an agent that is installed on this machine."""


def opted_in() -> bool:
    return os.environ.get(OPT_IN) == "1"


def allow(directory: str | Path) -> None:
    """Count one more directory, such as pytest's own base temporary one, as a fake's home."""
    real = os.path.realpath(directory)
    if real not in ROOTS:
        ROOTS.append(real)


def inside(path: str | Path, roots: Iterable[str] | None = None) -> bool:
    """Whether `path` lives under one of the directories a fake may live in."""
    real = os.path.realpath(path)
    where = (os.path.realpath(root) for root in (ROOTS if roots is None else roots))
    return any(real == root or real.startswith(root + os.sep) for root in where)


def fakes_only(resolve: Callable[[], str | None]) -> Callable[[], str | None]:
    """`resolve_binary` that finds only what lives where a fake does."""

    def resolve_binary() -> str | None:
        found = resolve()
        return found if found is not None and inside(found) else None

    return resolve_binary


def real_agent(
    program: Any,
    cwd: Any = None,
    env: Mapping[Any, Any] | None = None,
    roots: Iterable[str] | None = None,
) -> str | None:
    """The path of an agent on this machine that starting `program` would run, else `None`.

    Only a program named like an agent, that exists, and lives outside the
    directories a fake may: what is not there starts nothing, and what is
    somebody's `python` or `git` is none of this guard's business.
    """
    if program is None:
        return None
    try:
        text = os.fsdecode(program)
    except TypeError:
        return None
    if os.path.basename(text) not in AGENT_NAMES:
        return None
    if os.sep in text:
        base = os.fsdecode(cwd) if cwd is not None else os.getcwd()
        path: str | None = text if os.path.isabs(text) else os.path.join(base, text)
    else:
        path = shutil.which(text, path=_search_path(env))
    if path is None:
        return None
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        return None
    return None if inside(real, roots) else real


def _search_path(env: Mapping[Any, Any] | None) -> str | None:
    """The `PATH` the child would be started with."""
    if env is None:
        return None
    for key, value in env.items():
        if os.fsdecode(key) == "PATH":
            return os.fsdecode(value)
    return None


def _programs(event: str, args: tuple[Any, ...]) -> tuple[list[Any], Any, Any]:
    """What an audit event starts: the programs it names, the directory and the environment."""
    if event == "subprocess.Popen":
        executable, argv, cwd, env = args
        return [executable, argv[0] if argv else None], cwd, env
    if event == "os.spawn":
        _, path, argv, env = args
        return [path, argv[0] if argv else None], None, env
    path, argv, env = args
    return [path, argv[0] if argv else None], None, env


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event not in SPAWNS or opted_in():
        return
    try:
        programs, cwd, env = _programs(event, args)
    except (TypeError, ValueError, IndexError):
        return
    for program in programs:
        found = real_agent(program, cwd, env)
        if found is not None:
            message = (
                f"a test started the agent installed at {found}. Point the test at a fake "
                f"in tmp_path (RC_<AGENT>_BIN, or PATH), or set {OPT_IN}=1 for a test that "
                "means to drive the real agents"
            )
            VIOLATIONS.append(message)
            raise RealAgentStarted(message)


def hide_agents(monkeypatch: Any) -> None:
    """Make every agent look uninstalled, which holds under `RC_REAL_AGENTS=1` as well.

    For a test that detects every agent and has no fake of its own to offer.
    """
    from rc_client.agents.claude import runtime as claude
    from rc_client.agents.codex import runtime as codex
    from rc_client.agents.grok import runtime as grok
    from rc_client.agents.pi import runtime as pi

    for runtime in (claude, codex, grok, pi):
        monkeypatch.setattr(runtime, "resolve_binary", lambda: None)


_installed = False


def install() -> None:
    """Add the audit hook. Once it is added it stays for the life of the process."""
    global _installed
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
