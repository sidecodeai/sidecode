"""What a request for quota is answered with for an agent that could not answer it (A33).

The apps wait twenty seconds for `device.agents`, so an agent that has answered before gets
`AGENT_TIMEOUT` to do so again, and one that is slower, or fails, is reported as it last was. Its
accounts follow the rule of every earlier reading (`earlier_reading`): the windows last read are
shown, with no line over them, while they may stand in. An account with nothing recent enough to
show says that the device could not check the agent, if it is one that normally carries quota, and
any other stays as it was, as does one whose vendor truthfully reported no windows.

What an agent last said of each account's quota is kept account by account (`remember`), so that
an answer that only has an error for an account does not take away the windows an earlier answer
had read for it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from ..models import AgentAccount, AgentInfo, AgentLimit
from .earlier_reading import standing_in

SLOW_AGENT = "the device was too slow to check this agent, so its quota was not read"
FAILED_AGENT = "the device could not check this agent, so its quota was not read"
# The (agent, vendor) pairs whose account normally carries quota windows, or a line about why not,
# so that a request nobody answered can say so. Grok's has windows in leader mode alone, and a pi
# sign-in to any other vendor has none, so those say nothing.
QUOTA_ACCOUNTS = frozenset({("claude", "anthropic"), ("pi", "anthropic"), ("codex", "openai")})

# Whose account it is: the agent, the vendor, how it signs in and the email, if it has one.
Identity = tuple[str, str, str, str]
# An identity and the place of the account among those of the same: nothing else tells apart
# accounts that share one, such as the two sign-ins pi can hold to one vendor.
Slot = tuple[Identity, int]


def stand_in(info: AgentInfo, quota: AgentInfo | None, now: int, reason: str) -> AgentInfo:
    """`info` as the answer to a request for quota at `now` (protocol time), for `reason`.

    `quota` is what the agent last said of each account's quota (`remember`), windows and the time
    they were read at included, or nothing when it never has.
    """
    if info.accounts is None:
        return info
    earlier = dict(_slots(quota)) if quota is not None else {}
    accounts = [
        _account(info.agent, account, earlier.get(slot), now, reason)
        for slot, account in _slots(info)
    ]
    return replace(info, accounts=accounts)


def remember(previous: AgentInfo | None, latest: AgentInfo) -> AgentInfo:
    """What the agent says of each account's quota in `latest`, and has said in `previous`.

    An answer that has only an error for an account does not take away the windows an earlier
    answer had read for it: they stay, with the time they were read at, for `stand_in` to judge. An
    answer that says there are none, or that there is no way to read them, replaces them.
    """
    if previous is None or latest.accounts is None:
        return latest
    older = dict(_slots(previous))
    accounts = [_kept(account, older.get(slot)) for slot, account in _slots(latest)]
    return replace(latest, accounts=accounts)


def _slots(info: AgentInfo) -> list[tuple[Slot, AgentAccount]]:
    seen: Counter[Identity] = Counter()
    slots: list[tuple[Slot, AgentAccount]] = []
    for account in info.accounts or []:
        identity = (info.agent, account.provider, account.method, account.email or "")
        slots.append(((identity, seen[identity]), account))
        seen[identity] += 1
    return slots


def _kept(account: AgentAccount, before: AgentAccount | None) -> AgentAccount:
    if before is not None and _has_windows(before) and account.limits_error and not account.limits:
        return before
    return account


def _account(
    agent: str, account: AgentAccount, earlier: AgentAccount | None, now: int, reason: str
) -> AgentAccount:
    if account.method != "account":
        return account
    if earlier is None:
        return (
            _says(account, now, reason) if (agent, account.provider) in QUOTA_ACCOUNTS else account
        )
    if not earlier.limits and earlier.limits_error is None:
        return account
    windows = _recent(account, earlier, now)
    if windows:
        return replace(account, limits=windows, limits_checked_at=earlier.limits_checked_at)
    return _says(account, now, reason)


def _recent(account: AgentAccount, earlier: AgentAccount, now: int) -> list[AgentLimit]:
    """The windows `earlier` read that may be shown for `account`, which must have an email.

    Nothing but the email tells a sign-in from the one before it, so an account with none is
    never given windows: they could be those of somebody else's.
    """
    if not account.email or not earlier.limits or earlier.limits_checked_at is None:
        return []
    return standing_in(earlier.limits, earlier.limits_checked_at, now)


def _has_windows(account: AgentAccount) -> bool:
    return bool(account.limits) and account.limits_checked_at is not None


def _says(account: AgentAccount, now: int, reason: str) -> AgentAccount:
    return replace(account, limits_error=reason, limits_checked_at=now)
