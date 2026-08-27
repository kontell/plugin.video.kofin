"""What a full sync needs from the Library it runs for (P2.2).

``FullSync`` used to reach into the Library through nine duck-typed
attributes, and every test file faked a different slice of them. This is
that slice, named: the two database locks, the one-sync-at-a-time claim,
the three enqueue entry points the prune plans into, the refresh, the
watermark stamp, the playlist-poll deferral and the failure-toast set.
``Library.sync_host()`` builds one; ``tests/unit/synchost.py`` fakes one.
"""

from typing import Any, Iterable, Set


class SyncHost:
    def __init__(self, library: Any) -> None:
        self._library = library

    @property
    def database_lock(self):
        """MyVideos writes: one process-wide writer at a time."""
        return self._library.database_lock

    @property
    def music_database_lock(self):
        return self._library.music_database_lock

    def claim(self) -> bool:
        """Take the one-sync-at-a-time claim; False when one is already up.

        The claim lives on the Library rather than on FullSync (where the
        fork kept it, in a class-level Borg dict) because it must die with
        the manager that owns it: a service restart builds a fresh Library,
        and a claim that outlived the old one refused every sync the new one
        tried (audit finding #11).
        """
        return bool(self._library.claim_full_sync())

    def release(self) -> None:
        self._library.release_full_sync()

    def added(self, ids: Iterable[str]) -> None:
        self._library.added(list(ids))

    def updated(self, ids: Iterable[str]) -> None:
        self._library.updated(list(ids))

    def removed(self, ids: Iterable[str]) -> None:
        self._library.removed(list(ids))

    def refresh_libraries(
        self, databases: Iterable[str], force_reload: bool = False
    ) -> None:
        self._library.refresh_libraries(set(databases), force_reload=force_reload)

    def stamp_watermark_if_empty(self) -> None:
        self._library.stamp_watermark_if_empty()

    def defer_playlist_poll(self) -> None:
        self._library.defer_playlist_poll()

    @property
    def failure_toasted(self) -> Set[str]:
        """Libraries already toasted as failed this service lifetime."""
        toasted: Set[str] = self._library.sync_failure_toasted
        return toasted
