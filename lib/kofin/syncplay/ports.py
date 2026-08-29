"""What SyncPlay needs from the REST client (shell refactor P1.2).

``SyncPlayManager`` reaches ``core.api.Api`` through getattr-by-name
dispatch (``_api``/``_api_raw``), which left every verb below with zero
static callers — a rename in ``core/api.py`` surfaced as a logged
"SyncPlay x failed" at runtime. This Protocol is the static caller: the
manager's constructor and ``get_api`` are typed against it, so ``Api``'s
conformance is checked where the service hands the client over
(``service/main.py``) and a renamed verb fails mypy instead.

The dispatch itself stays ``getattr`` — the transplant keeps its shape;
only its target is now a checked surface.
"""

from typing import Any, Dict, List, Optional, Protocol

JsonDict = Dict[str, Any]


class SyncPlayApi(Protocol):
    """The SyncPlay and session verbs, plus the two socket helpers the
    time-sync transport needs. Signatures mirror ``core.api.Api``."""

    def get_utc_time(self) -> JsonDict: ...

    def item(self, item_id: str) -> JsonDict: ...

    def websocket_url(self, path: str) -> str: ...

    def authorization(self) -> str: ...

    def syncplay_list(self) -> List[JsonDict]: ...

    def syncplay_new(
        self, group_name: str, protocol_version: Optional[int] = None
    ) -> None: ...

    def syncplay_join(
        self, group_id: str, protocol_version: Optional[int] = None
    ) -> None: ...

    def syncplay_leave(self) -> None: ...

    def syncplay_hello(self, protocol_version: int) -> JsonDict: ...

    def syncplay_snapshot(self) -> None: ...

    def syncplay_ready(
        self, when: str, position_ticks: int, is_playing: bool, playlist_item_id: str
    ) -> None: ...

    def syncplay_buffering(
        self, when: str, position_ticks: int, is_playing: bool, playlist_item_id: str
    ) -> None: ...

    def syncplay_ping(self, ping_ms: int) -> None: ...

    def syncplay_unpause(self) -> None: ...

    def syncplay_pause(self) -> None: ...

    def syncplay_stop(self) -> None: ...

    def syncplay_seek(self, position_ticks: int) -> None: ...

    def syncplay_set_new_queue(
        self,
        item_ids: List[str],
        playing_item_position: int = 0,
        start_position_ticks: int = 0,
    ) -> None: ...

    def syncplay_set_playlist_item(self, playlist_item_id: str) -> None: ...

    def syncplay_queue(self, item_ids: List[str], mode: str = "Queue") -> None: ...

    def syncplay_next_item(self, playlist_item_id: str) -> None: ...

    def syncplay_previous_item(self, playlist_item_id: str) -> None: ...

    def syncplay_set_ignore_wait(self, ignore_wait: bool) -> None: ...
