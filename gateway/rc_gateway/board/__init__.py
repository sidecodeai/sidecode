"""A task board for each conversation (A62).

The gateway keeps, for each session an app of the account opened, a short list of what the person
asked for and where each request stands, made by a language model from the conversation's recent
rounds, which the gateway reads from the device itself. The account preference ``task_board`` is
the permission: nothing is read or kept while it is off. Who may have boards, and on whose model,
is a policy (``policy.py``): a self-hosted gateway uses the operator's polish provider with its own
``BOARD_MODEL``, and a gateway that bills its model calls passes a policy of its own. This package
imports none of it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI

from ..state import GatewayState
from .config import BoardConfig, load_board_config
from .llm import ChatModel
from .policy import BoardPolicy, OperatorPolicy
from .service import BoardService
from .store import BoardStore

STORE_FILE = "boards.sqlite3"


def mount_board(
    app: FastAPI,
    state: GatewayState,
    *,
    config: BoardConfig | None = None,
    policy: BoardPolicy | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BoardService | None:
    """Keep task boards when the feature is configured, under ``policy`` (the operator's if none).

    A gateway with a hosted service bills its model calls: without a policy that bills them the
    boards stay off. A state with no such service is a self-hosted one, and keeps boards when the
    polish provider and ``BOARD_MODEL`` are both set.
    """
    if getattr(state, "paid", None) is not None and policy is None:
        return None
    if policy is None:
        resolved = config if config is not None else load_board_config()
        polish = state.config.polish
        if not (polish.enabled and resolved.model):
            return None
        policy = OperatorPolicy(ChatModel(polish.base_url, polish.api_key, resolved.model))
    service = BoardService(
        state.hub,
        state.index,
        BoardStore(state.config.db_path(STORE_FILE)),
        state.preference_store,
        policy,
        clock=clock,
        sleep=sleep,
    )
    state.board = service
    state.hub.board = service
    app.state.board = service
    return service
