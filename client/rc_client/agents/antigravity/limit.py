"""Whether an Antigravity turn ended at the account's usage limit (A35, A64, 7.2).

`agy`'s words are never the signal: they only make the device ask `agy`'s own usage query afresh,
and the turn ends with a limit only when that query reports a window with nothing left — in the
group whose models include the session's, or in any group when none names it. The words that set
the query going are the ones `agy` was seen to carry for a used-up quota; they stay where `agy`
put them.
"""

from __future__ import annotations

from ...logging_setup import logger
from ...sessions.limits import LimitStop
from .usage import Group, Reading, Usage, UsageFailed

log = logger("rc_client.antigravity.limit")

# The phrases `agy` and its backend use for a used-up quota, matched without case.
QUOTA_WORDS = (
    "exhausted your quota",
    "quota_exhausted",
    "resource_exhausted",
    "individual quota reached",
    "usage limit reached",
)


def reads_like_quota(*texts: str) -> bool:
    """Whether any of `texts` reads like `agy`'s quota message, which only asks the query."""
    lowered = " ".join(texts).lower()
    return any(words in lowered for words in QUOTA_WORDS)


async def stop(usage: Usage, binary: str, model_label: str | None) -> LimitStop | None:
    """The limit a fresh usage query shows used up, or None when it shows none, or cannot say."""
    try:
        reading = await usage.read(binary, fresh=True)
    except UsageFailed as failed:
        log.info("the usage query could not confirm a limit", reason=failed.reason)
        return None
    return used_up(reading, model_label)


def used_up(reading: Reading, model_label: str | None) -> LimitStop | None:
    """The window with nothing left, the later reset of two; None while every one has some."""
    groups = [group for group in reading.groups if _names(group, model_label)] or list(
        reading.groups
    )
    spent = [bucket for group in groups for bucket in group.buckets if bucket.remaining <= 0.0]
    if not spent:
        return None
    last = max(spent, key=lambda bucket: bucket.resets_at or 0)
    return LimitStop(window_minutes=last.minutes, resets_at_ms=last.resets_at)


def _names(group: Group, label: str | None) -> bool:
    """Whether the group lists the model: by its name, or the name its variant label starts with."""
    if not label:
        return False
    return any(label == name or label.startswith(f"{name} ") for name in group.models)
