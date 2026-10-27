"""Amendment A55: rename a Grok session through the leader's `x.ai/session/rename`.

Grok Build renames a session with `/rename <title>`: the title goes into
`summary.json` marked `title_is_manual`, so that its own titling never replaces it.
A client asks for the same through an extension method that only the leader serves,
so a session is renamed through the leader or not from here.

Nothing below has been seen on the wire. It is read from the strings of Grok Build
1.0.70's binary and stays unverified until it is checked against a real leader, which
is why it is written down once, in this module:

- the wire name is `_x.ai/session/rename`, spelled as `_x.ai/session/info` and
  `_x.ai/session/list` are where the binary has `x.ai/session/info` and
  `x.ai/session/list`;
- the request is a `SessionRenameRequest` of five fields, four of them visible:
  `sessionId`, `title`, `cwd` and `resetToAuto`. `resetToAuto` is never sent, since
  clearing a name is not part of A55, and the fifth field is not known;
- the answer's shape is not known, so none of it is read: the name Grok holds is
  taken to be the one it was given, and `_x.ai/sessions/changed` says otherwise;
- what the binary has for a refusal of the name is "title must not be blank",
  "title too long (max N characters ...)" and "title too large".
"""

from __future__ import annotations

from typing import Any

from ...errors import RcError
from .acp import GrokError

METHOD = "_x.ai/session/rename"
# The reply is waited for no longer than the leader's other quick questions are.
TIMEOUT = 15.0
# The JSON-RPC code for a request whose fields were refused.
INVALID_PARAMS = -32602
# What the leader says when it refuses the name itself, in the words of the
# binary's strings. Any other error that merely mentions a title, a failure to
# write it among them, is not the name's fault.
REJECTED = ("title must not be blank", "title too long", "title too large")

NOT_JOINED = "grok can only be renamed through its leader, which is not connected"
NOT_TAKEN = "grok did not take the new name; rename it in the terminal"


def params(session_id: str, cwd: str, title: str) -> dict[str, Any]:
    """The fields of the request, the one place they are spelled."""
    return {"sessionId": session_id, "title": title, "cwd": cwd}


def refusal(error: GrokError) -> RcError:
    """What the leader's answer means for a rename, said in words of our own.

    The name is the person's to change only when the leader clearly rejects it:
    invalid params, or words that say it is blank or too long. Everything else, a
    leader that does not know the method, a session it does not hold, a failure
    inside it, leaves the rename to the terminal.
    """
    words = f"{error.detail or ''} {error.message}".lower()
    if error.rpc_code == INVALID_PARAMS or any(phrase in words for phrase in REJECTED):
        return RcError("bad_request", "grok does not accept this name")
    return RcError("unsupported", NOT_TAKEN)
