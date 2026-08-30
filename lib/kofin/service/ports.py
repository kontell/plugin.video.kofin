"""The service's view of its managers, and theirs of the service (P1.3).

``service/main.py`` holds one slot per manager and used to type all three
``Optional[Any]``, which left every member access unchecked — 32 of them,
and seven ``type: ignore[attr-defined]`` in ``settings_apply.py`` for the
trip back. These Protocols are the seams, checked where the concrete
objects flow in: ``Library``/``DownloadManager``/``SyncPlayManager`` at
their construction sites in ``main.py``, and ``Service`` itself where it
hands ``self`` to ``SettingsApplier``.

``LibraryPort`` also restates the port a full sync speaks (the note above
``Library.claim``; ``tests/unit/synchost.py`` is its one fake), so the
whole consumed surface of ``Library`` is written down in one place.
"""

import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Set

from kofin.core.log import Logger

LOG = Logger(__name__)


class LibraryPort(Protocol):
    """What the service drives on the library manager (a Thread), plus the
    FullSync port below the divider."""

    startup_done: bool
    stop_thread: bool

    @property
    def ident(self) -> Optional[int]: ...

    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def join(self, timeout: Optional[float] = None) -> None: ...

    def workers_alive(self) -> bool: ...

    def stop_client(self) -> None: ...

    def enqueue_command(
        self, command: str, data: Optional[Dict[str, Any]] = None
    ) -> None: ...

    def userdata(self, data: List[Dict[str, Any]]) -> None: ...

    # -- what a full sync needs (see the note above Library.claim) ----------

    database_lock: threading.Lock
    music_database_lock: threading.Lock
    sync_failure_toasted: Set[str]

    def claim(self) -> bool: ...

    def release(self) -> None: ...

    def added(self, ids: Iterable[str]) -> None: ...

    def updated(self, ids: Iterable[str]) -> None: ...

    def removed(self, ids: Iterable[str]) -> None: ...

    def refresh_libraries(
        self, databases: Iterable[str], force_reload: bool = ...
    ) -> None: ...

    def stamp_watermark_if_empty(self) -> None: ...

    def defer_playlist_poll(self) -> None: ...


class DownloadsPort(Protocol):
    """What the service drives on the download manager."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def wake(self) -> None: ...

    def submit(
        self,
        item_ids: List[str],
        origin: str = ...,
        media_types: Optional[List[str]] = None,
    ) -> None: ...

    def cancel(self, item_id: str) -> None: ...

    def remove(self, item_ids: List[str]) -> None: ...

    def remove_all(self) -> None: ...


class SyncPlayPort(Protocol):
    """What the service drives on the SyncPlay manager — directly, and the
    hook names ``forward`` reaches by string (on_wake/on_sleep/on_kodi_play)."""

    def stop(self) -> None: ...

    def on_notification(self, method: str, data: Any) -> None: ...

    def refresh_tempo_session(self) -> None: ...

    def on_wake(self) -> None: ...

    def on_sleep(self) -> None: ...

    def on_kodi_play(self, data: Any) -> None: ...


class ServiceHooks(Protocol):
    """What ``SettingsApplier`` reaches back into the service for."""

    _restart_requested: bool
    _online: bool
    library: Optional[LibraryPort]
    syncplay: Optional[SyncPlayPort]

    def _start_library(self) -> None: ...

    def _start_syncplay(self) -> None: ...

    def _stop_syncplay(self) -> None: ...

    def _start_downloads(self) -> None: ...

    def _stop_downloads(self) -> None: ...

    def _start_backdrop(self, force: bool = ...) -> None: ...


def forward(manager: Optional[object], name: str, *args: Any) -> None:
    """Call a manager hook by name without letting it break the caller —
    the one shape behind ``main._syncplay_forward`` and
    ``player._syncplay_event``."""
    if manager is None:
        return
    try:
        getattr(manager, name)(*args)
    except Exception:
        LOG.exception("SyncPlay %s hook failed", name)


def spawn_once(
    current: Optional[threading.Thread],
    target: Callable[..., None],
    name: str,
    *args: Any,
) -> Optional[threading.Thread]:
    """Start a named one-shot daemon worker unless the previous one still
    runs; returns the new thread, or None when busy (P1.10). The caller
    keeps the slot — and its own busy message. ``current=None`` is the
    fire-and-forget case (the player's prompt threads)."""
    if current is not None and current.is_alive():
        return None
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread
