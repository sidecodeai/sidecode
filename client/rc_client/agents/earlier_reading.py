"""When an earlier reading of a quota may stand in for one that cannot be made (A33).

A vendor that cannot answer just now, or an agent too slow to ask it, must not blank the meters.
But a meter that is old is more likely wrong than right, and the apps draw one without saying when
it was read. So only windows read under half an hour ago may stand in, and only those that have
not reset since. Every place that falls back on an earlier reading asks here.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import AgentLimit

# How old a reading may be, in protocol milliseconds, and still stand in.
MAX_AGE_MS = 30 * 60 * 1000


def standing_in(limits: Iterable[AgentLimit], checked_at: int, now: int) -> list[AgentLimit]:
    """The windows of a reading made at `checked_at` that may be shown at `now`.

    None when the reading is too old. A window whose reset has passed is left out, because its
    percentage is certainly out of date; one that names no reset time is kept, as nothing says
    that it is.
    """
    if now - checked_at >= MAX_AGE_MS:
        return []
    return [limit for limit in limits if limit.resets_at is None or limit.resets_at > now]
