"""A55: the one private SDK call the rename of a driven Claude session depends on."""

from __future__ import annotations

import importlib.metadata
import inspect

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk._internal.query import Query

# The SDK this device was checked against. `ClaudeRunner.rename_session` sends the
# CLI's `rename_session` control request through `ClaudeSDKClient._query`, which is
# private and not in the SDK's documented surface (its `rename_session` helper only
# writes a transcript row). An upgrade is therefore a re-check, not a bump.
CHECKED_SDK = "0.2.152"


def test_the_sdk_is_the_one_the_private_request_was_checked_against() -> None:
    installed = importlib.metadata.version("claude-agent-sdk")
    assert installed == CHECKED_SDK, (
        f"claude-agent-sdk is {installed}, not {CHECKED_SDK}: check that "
        "`ClaudeSDKClient._query._send_control_request(request, timeout=)` still exists and that "
        "the CLI it drives still answers a `rename_session` request (rc-client "
        "`ClaudeRunner.rename_session`), then update CHECKED_SDK"
    )


def test_the_client_holds_a_query_object_that_sends_control_requests() -> None:
    client = ClaudeSDKClient()
    assert hasattr(client, "_query")
    assert list(inspect.signature(Query._send_control_request).parameters) == [
        "self",
        "request",
        "timeout",
    ]
    assert inspect.iscoroutinefunction(Query._send_control_request)
