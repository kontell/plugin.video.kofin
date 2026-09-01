"""The provider seam: how the engine starts content and maps local plays.

G1 of ``docs/syncplay-generic-backend-plan.md``. The engine coordinates the
*global* Kodi player; what a queue item id means — how to start it at a
position, and how a Kodi library id maps back to one — is the owning
provider's business. Today there is exactly one provider,
Jellyfin-through-kofin, and this seam exists so there can be another without
the engine changing shape (``docs/syncplay-generic-backend-feasibility.md``
§5).

Kept in the ``ports.py`` discipline: the engine types against the Protocol,
the registry is the only dispatch, and the Jellyfin implementation is the one
place the engine's start path may reach kofin's plugin URL builder and sync
database.
"""

from typing import Dict, Optional, Protocol, TypedDict
from urllib.parse import quote

from kofin.core.urls import plugin_url
from kofin.syncplay.ports import SyncPlayApi

#################################################################################################

JELLYFIN = "jellyfin"


class PlayTarget(TypedDict):
    """What the playback controller needs to start a queue item: the URL to
    hand Kodi, and which playlist it belongs on."""

    url: str
    audio: bool


class Provider(Protocol):
    """What the engine needs from a content provider (plan G1.1)."""

    def play_target(self, key: str, start_ticks: int) -> PlayTarget: ...

    def resolve_kodi_id(self, kodi_id: int, media: str) -> Optional[str]: ...


#################################################################################################


class JellyfinProvider:
    """Jellyfin-through-kofin: the reference provider.

    ``api`` mirrors the manager's Optional client: a manager built without
    one answers every start with a failed lookup, exactly as the getattr
    dispatch answered None before the seam existed.
    """

    def __init__(self, api: Optional[SyncPlayApi]):
        self.api = api

    def play_target(self, key: str, start_ticks: int) -> PlayTarget:
        # Direct on the client, not via the manager's _api wrapper: a 403
        # here is a library permission problem, not lost group membership.
        if self.api is None:
            raise LookupError("no API client")

        item = self.api.item(key)

        if not item:
            raise LookupError("item lookup failed")

        params = {"mode": "play", "id": str(key)}

        # Always sent, and never negative. A group start names the position
        # even when that position is zero, so a falsy 0 must not be dropped —
        # omitting it lets the play route fall back to the member's own resume
        # point, which starts it minutes away from the group. And the estimate
        # can land just below zero (extrapolation across a clock offset), which
        # was measured reaching the route as startticks=-240000.
        params["startticks"] = str(max(0, int(start_ticks or 0)))

        return {"url": plugin_url(params), "audio": item.get("Type") == "Audio"}

    def resolve_kodi_id(self, kodi_id: int, media: str) -> Optional[str]:
        from kofin.sync import db as database  # deferred: pulls in the DB stack

        mapped = database.get_item(kodi_id, media)
        return mapped[0] if mapped else None


#################################################################################################


class TemplateProvider:
    """A provider registered over the public bus (plan G2.4): start-only.

    The template names ``{key}`` and optionally ``{position_s}`` (whole
    seconds). Substitution is by token replace, never str.format — the
    template is foreign text and must not be able to name anything else.
    ``resolve_kodi_id`` answers None: a foreign provider has no claim on
    the Kodi library; its plays are identified by the claims it publishes.
    """

    def __init__(self, template: str, audio: bool = False):
        self.template = template
        self.audio = audio

    def play_target(self, key: str, start_ticks: int) -> PlayTarget:
        seconds = max(0, int(start_ticks or 0)) // 10000000
        url = self.template.replace("{key}", quote(key, safe="")).replace(
            "{position_s}", str(seconds)
        )
        return {"url": url, "audio": self.audio}

    def resolve_kodi_id(self, kodi_id: int, media: str) -> Optional[str]:
        return None


class ProviderRegistry:
    """The engine's one dispatch from a content key to its provider.

    G1 knows a single provider; G2's ``SyncProvider.Register`` intake is what
    grows this beyond ``jellyfin``, and the queue side stays keyed by the
    default until G3's descriptors name a provider per entry.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, Provider] = {}

    def register(self, name: str, provider: Provider) -> None:
        self._providers[name] = provider

    def get(self, name: str = JELLYFIN) -> Provider:
        return self._providers[name]

    def play_target(
        self, key: str, start_ticks: int, provider: str = JELLYFIN
    ) -> PlayTarget:
        return self.get(provider).play_target(key, start_ticks)

    def resolve_kodi_id(self, kodi_id: int, media: str) -> Optional[str]:
        # The default provider owns the Kodi library today; a second library
        # owner joins this dispatch when one exists.
        return self.get(JELLYFIN).resolve_kodi_id(kodi_id, media)


def jellyfin_registry(api: Optional[SyncPlayApi]) -> ProviderRegistry:
    """The registry every kofin service starts with."""
    registry = ProviderRegistry()
    registry.register(JELLYFIN, JellyfinProvider(api))
    return registry
