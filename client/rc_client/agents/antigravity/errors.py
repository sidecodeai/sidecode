"""What the device answers and says for Antigravity, in its own words (A64, 4.4).

Each refusal carries the code an app branches on and a sentence it shows as it is. `agy`'s own
words reach an app only with every path cut down to a file name, as an agent's error always does.
"""

from __future__ import annotations

import os
import re

from ...attachments import without_paths
from ...errors import RcError
from . import runtime

# The JSON-RPC code `agy` answers with when it is not signed in.
AUTH_REQUIRED = -32000

NOT_INSTALLED = "antigravity is not installed on this device"
SIGNED_OUT = (
    "Antigravity is not signed in on this computer. "
    "Run agy in a terminal there and sign in, then try again."
)
TOO_OLD = (
    "Antigravity CLI {minimum} or later is needed; this computer has {version}. "
    "Run agy update there."
)
# An `agy` the device did not start may have the conversation open (4.4).
HELD = (
    "agy is running on this computer and may have this conversation open; "
    "quit agy there to continue here."
)
ALSO_OPEN = (
    "agy is running on this computer and may have this conversation open too; "
    "messages sent from both may conflict."
)
GONE = "Antigravity no longer has this conversation."
STOPPED = "Antigravity stopped before the turn finished."
NO_SPEED = "antigravity has no speed tiers"
DID_NOT_STOP = "the agent did not stop in time"
RECONNECTING = "reconnecting to the agent"
SCAN_FAILED = "could not tell whether agy has this conversation open; try again in a moment"
NOT_RUNNING = "the Antigravity session is not connected"
MAX_WORDS = 2000
# An absolute path or one from `~`, written into a sentence: everything up to what ends it.
_PATH = re.compile(r"(?<![\w:/.~-])~?/(?:[^\s/'\"`<>()\[\]{}]+/)+[^\s/'\"`<>()\[\]{}]*")


def words(text: str, limit: int = MAX_WORDS) -> str:
    """`agy`'s sentence with every path cut down to its last part, at most `limit` characters."""
    cut = without_paths(text)
    cut = _PATH.sub(lambda found: os.path.basename(found.group(0).rstrip("/")) or "/", cut)
    return cut.strip()[:limit]


def signed_out() -> RcError:
    return RcError("agent_unavailable", SIGNED_OUT)


def too_old(version: str) -> RcError:
    minimum = ".".join(str(part) for part in runtime.MIN_VERSION)
    return RcError("agent_unavailable", TOO_OLD.format(minimum=minimum, version=version))


def held() -> RcError:
    return RcError("conflict", HELD)


def gone() -> RcError:
    return RcError("not_found", GONE)


def scan_failed() -> RcError:
    return RcError("timeout", SCAN_FAILED)


def refusal(code: int | None, message: str) -> RcError:
    """What an app is told when `agy` refused to open or start something."""
    if code == AUTH_REQUIRED:
        return signed_out()
    return RcError("agent_unavailable", words(message) or "agy refused the request")
