"""The task board's provider by name (A65): a new name asks every account again.

An account turns its task board on after an app has named the provider its conversations go to.
The preference store records that name, ``BOARD_PROVIDER_NAME``, and a gateway that starts under
another one, an unset name on either side included, turns every account's task board off the way
the account itself would (A62): the preference is written, the account's apps and devices are told,
and its boards are deleted. Then every board left in the store goes too, such as one an account
kept after turning its board off while no board service ran. Each person then decides again, under
the new name. This runs whether or not the board is mounted, before the boards are followed again
and before the gateway serves anyone.
"""

from __future__ import annotations

from ..logging import logger
from ..state import GatewayState
from .store import STORE_FILE, BoardStore

log = logger("rc_gateway.board")


async def renew_board_consent(state: GatewayState) -> None:
    """Turn every account's task board off when the provider's name is not the one recorded."""
    preferences = state.preference_store
    current = state.config.provider_names.board
    if await preferences.board_provider() == current:
        return
    accounts = await preferences.board_accounts()
    for username in accounts:
        before = await preferences.get(username)
        after = await preferences.patch(username, {"task_board": False})
        await state.publish_preferences(username, before, after)
    path = state.config.db_path(STORE_FILE)
    if path.exists():
        await BoardStore(path).clear()
    # Recorded last: a start that stops half-way turns the rest off the next time.
    await preferences.record_board_provider(current)
    log.info("task boards turned off for a new provider", accounts=len(accounts))
