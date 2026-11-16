"""The agents the device last detected, kept for the start of its next run.

Detection asks every agent's CLI what it offers, and on a loaded machine that
took tens of seconds, so the link no longer waits for it (`Daemon.run`). The
gateway replaces a device's agents with whatever its `hello` lists, so a hello
sent before this run's detection lists what the last run found, and
`agents.updated` follows once this run knows. A device with nothing kept, on its
first run, gives detection a while before its first hello, as every hello used
to wait for it. Until an agent that needs no service of its own (Claude, pi) has
been asked again, the device also acts through what the last run found, so a
request for it need not wait at all.
"""

from __future__ import annotations

import json
from typing import Any, cast, get_args

from ..models import UNSET, AccountMethod, AgentAccount, AgentInfo, Attach, Choice
from ..registry import Registry

KEY = "agents.announced"


def load(registry: Registry) -> list[dict[str, Any]]:
    raw = registry.get_kv(KEY)
    try:
        listed = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(listed, list):
        return []
    return [item for item in listed if isinstance(item, dict)]


def save(registry: Registry, listed: list[dict[str, Any]]) -> None:
    registry.set_kv(KEY, json.dumps(listed, ensure_ascii=False))


def decode(item: dict[str, Any]) -> AgentInfo | None:
    """An agent as `AgentInfo.to_dict` wrote it; None for one that does not read as one."""
    agent = item.get("agent")
    if not isinstance(agent, str) or not agent:
        return None
    attach = item.get("attach")
    keys = item.get("shared_settings_keys")
    accounts = item.get("accounts")
    return AgentInfo(
        agent=agent,
        available=item.get("available") is True,
        version=_text(item.get("version")),
        path=_text(item.get("path")),
        models=_choices(item.get("models")),
        default_model=_text(item.get("default_model")),
        permission_modes=_choices(item.get("permission_modes")),
        default_permission_mode=_text(item.get("default_permission_mode")),
        efforts=_choices(item.get("efforts")),
        default_effort=_text(item.get("default_effort")),
        speeds=_choices(item.get("speeds")),
        capabilities=_texts(item.get("capabilities")),
        attach=cast(Attach, attach) if attach in get_args(Attach) else None,
        attach_ready=item.get("attach_ready") is True,
        shared_interrupt=item.get("shared_interrupt") is True,
        shared_settings=item.get("shared_settings") is True,
        shared_settings_keys=_texts(keys) if isinstance(keys, list) else None,
        shared_attachments=item.get("shared_attachments") is True,
        shared_steer=item.get("shared_steer") is True,
        accounts=_accounts(accounts) if isinstance(accounts, list) else None,
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _texts(value: Any) -> list[str]:
    return [each for each in value if isinstance(each, str)] if isinstance(value, list) else []


def _choices(value: Any) -> list[Choice]:
    if not isinstance(value, list):
        return []
    return [
        Choice(id=each["id"], label=each["label"])
        for each in value
        if isinstance(each, dict)
        and isinstance(each.get("id"), str)
        and isinstance(each.get("label"), str)
    ]


def _accounts(value: list[Any]) -> list[AgentAccount]:
    accounts: list[AgentAccount] = []
    for each in value:
        if not isinstance(each, dict) or not isinstance(each.get("provider"), str):
            continue
        method = each.get("method")
        if method not in get_args(AccountMethod):
            continue
        accounts.append(
            AgentAccount(
                provider=each["provider"],
                method=cast(AccountMethod, method),
                plan=each.get("plan", UNSET),
                tier=_text(each.get("tier")),
                email=_text(each.get("email")),
                endpoint=_text(each.get("endpoint")),
            )
        )
    return accounts
