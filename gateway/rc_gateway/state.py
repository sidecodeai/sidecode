"""The object every route and socket reads from.

FastAPI stores one ``GatewayState`` on ``app.state.gateway``; nothing else is global, so a test can
build a complete gateway on a temporary directory with injected senders.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import __version__
from .apns import ApnsProvider
from .auth_store import AuthSessionStore
from .client_dist import ServedClient, served_client
from .compat import apps_view
from .config import Config
from .devices import DeviceStore
from .hub import Hub
from .index import SessionIndex
from .pairing_requests import PairingRequests
from .polish import Polisher
from .preference_store import Preferences, PreferenceStore
from .provider_names import named
from .push import PushService
from .push_store import PushStore
from .ratelimit import RateLimiter
from .rejects import RejectionLog
from .session_registry import SessionRegistry
from .stt import Transcriber
from .users import UserStore

if TYPE_CHECKING:
    from .board.service import BoardService

VERSION = __version__


@dataclass
class GatewayState:
    config: Config
    devices: DeviceStore
    index: SessionIndex
    push_store: PushStore
    preference_store: PreferenceStore
    auth_store: AuthSessionStore
    users: UserStore
    sessions: SessionRegistry
    login_limiter: RateLimiter
    enroll_limiter: RateLimiter
    pairing_limiter: RateLimiter
    polish_limiter: RateLimiter
    stt_limiter: RateLimiter
    hub: Hub = field(init=False)
    push: PushService = field(init=False)
    transcriber: Transcriber | None = None
    polisher: Polisher | None = None
    #: The task boards (A62), when this gateway keeps them; mounted by ``board.mount_board``.
    board: BoardService | None = None
    apns: ApnsProvider | None = None
    #: Refused `/ws/device` upgrades, so an orphaned daemon is visible without flooding the log.
    device_rejects: RejectionLog = field(default_factory=RejectionLog)
    #: The same flood valve for the two app-facing upgrades, which had none.
    app_rejects: RejectionLog = field(default_factory=RejectionLog)
    stt_rejects: RejectionLog = field(default_factory=RejectionLog)
    #: Claim tokens waiting to be scanned (A23). Memory only: a restart forgets them.
    pairing_requests: PairingRequests = field(default_factory=PairingRequests)
    #: Live `/ws/stt` sockets per account (A29 neighbours it): transcription spends the operator's
    #: speech credit, so one account cannot hold an unbounded number of streams open.
    stt_sockets: Counter[str] = field(default_factory=Counter)

    def stt_view(self) -> dict[str, Any]:
        """The `stt` object of `GET /api/config` and of the app `hello`, one for both."""
        stt = self.config.stt
        # A44: the provider detects the language of every transcription, so there is nothing to
        # offer; the list stays on the wire, always `auto`, for the apps that read it. A57: an app
        # reads `streaming` to choose between live partials and a transcript shown when it is done.
        # A65: the provider's name, when the operator gave one.
        view = {"enabled": stt.enabled, "languages": ["auto"], "streaming": stt.streaming}
        return named(view, self.config.provider_names.stt)

    def polish_view(self) -> dict[str, Any]:
        return named({"enabled": self.config.polish.enabled}, self.config.provider_names.polish)

    def board_view(self) -> dict[str, Any]:
        """A62: the `board` object of `GET /api/config` and of the app `hello`."""
        return named({"enabled": self.board is not None}, self.config.provider_names.board)

    def apps_view(self) -> dict[str, Any]:
        """A31, A45: the oldest build of each separately installed app this gateway works with."""
        return apps_view(self.config)

    async def preferences_view(self, username: str) -> dict[str, Any]:
        """A35: the switches one account reads, whether it has ever set one or not."""
        return (await self.preference_store.get(username)).view()

    async def device_preferences_view(self, username: str) -> dict[str, Any]:
        """The same for the account's devices, without what only the apps read (A62)."""
        return (await self.preference_store.get(username)).device_view()

    async def publish_preferences(
        self, username: str, before: Preferences, preferences: Preferences
    ) -> None:
        """Announce a change to everything the account has connected (A35).

        The apps need it to redraw the switch they did not flip; the devices need it to act on,
        which is the whole point of storing it here rather than in an app. The task board acts
        on its own switch turning on or off (A62).
        """
        await self.hub.broadcast_user(
            username, {"type": "preferences.updated", "preferences": preferences.view()}
        )
        await self.hub.send_to_devices(
            username, {"type": "preferences", "preferences": preferences.device_view()}
        )
        if self.board is not None and before.board_on != preferences.board_on:
            await self.board.on_preferences(username, preferences)

    def client_view(self) -> dict[str, Any] | None:
        """The served wheel (A22), or None in a checkout where none has been built."""
        served = self.served_client()
        if served is None:
            return None
        return {"version": served.version, "build": served.build, "url": served.url}

    def served_client(self) -> ServedClient | None:
        return served_client(self.config.client_dist_dir)

    def served_build(self) -> str | None:
        """The build every device is brought to (A36); None where no wheel has been built."""
        served = self.served_client()
        return None if served is None else served.build

    def config_view(self) -> dict[str, Any]:
        view: dict[str, Any] = {
            "public_origin": self.config.public_origin,
            "stt": self.stt_view(),
            "polish": self.polish_view(),
            "board": self.board_view(),
            "apps": self.apps_view(),
            "push": {
                "web_enabled": self.push.web_enabled,
                "apns_enabled": self.push.apns_enabled,
            },
            "version": VERSION,
        }
        # A22. The schema makes `client` required, and there is no honest value for a build that
        # does not exist, so a gateway with no wheel omits the field: no wheel, no update to offer.
        client = self.client_view()
        if client is not None:
            view["client"] = client
        return view

    async def device_name(self, device_id: str) -> str:
        record = await self.devices.get(device_id)
        return record.name if record is not None else device_id

    async def badge_count(self, username: str) -> int:
        """A47: how many of the account's sessions show `unseen`, its icon's badge."""
        owned = [record.device_id for record in await self.devices.list_for_user(username)]
        return await self.index.count_unseen(owned)
