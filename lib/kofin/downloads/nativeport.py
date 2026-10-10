"""The library side of a download, per build.

A download is a file on disk and a row in kofin's own store; what the Kodi
library says about it differs by build. The SQL build relocates Kodi's file
rows onto the local file and stamps the badge and the tag itself
(downloads/repoint.py, sync/kodidb/downloads.py). The API build may not
touch Kodi's databases: its rows keep their plugin URLs, the resolver plays
the local file for a downloaded item however it is reached (plugin/play.py),
and the badge and the tag reach the row through the catalogue's pass, which
re-plans the item with its download state as an input
(sync/backends/api/downloaded.py). Kodi's own play state is read back
through JSON-RPC by the mapped id, so the retention sweep still works
offline, and the local watched mark of a vanished download is left to the
server round-trip that already follows it.
"""

from datetime import datetime
from typing import Any, Iterable, List, Optional, Protocol, Tuple

from kofin import buildconfig
from kofin.core.log import Logger

LOG = Logger(__name__)


class Native(Protocol):
    def attached(self, row: Any, root: str) -> bool: ...

    def detached(self, row: Any) -> None: ...

    def restore(self, row: Any, root: str) -> bool: ...

    def mapped(self, item_id: str) -> bool: ...

    def mark_watched(self, row: Any) -> None: ...

    def watched_locally(self, row: Any) -> bool: ...

    def last_touch(self, row: Any) -> Optional[Tuple[float, bool]]: ...

    def music_view(self, force: bool) -> None: ...

    def refresh_song_playlists(self, item_ids: Iterable[str]) -> None: ...


def for_build() -> Native:
    if buildconfig.legacy_features():
        return NativeSql()
    return NativeApi()


def _as_epoch(stamp: Any) -> float:
    """A Kodi timestamp column as unix seconds; 0.0 for unset or unparseable.

    Two spellings reach the column and both are local time: Kodi's own (and
    the SQL port's) ``'%Y-%m-%d %H:%M:%S'``, and the ISO ``T`` form the sync
    writers hand it (``shims.date_played``). 0.0 rather than an exception for
    anything else: a column kofin did not write is not a reason to abandon a
    sweep.
    """
    if not stamp:
        return 0.0
    try:
        return datetime.strptime(
            str(stamp)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"
        ).timestamp()
    except ValueError:
        return 0.0


class NativeSql:
    """The SQL build: Kodi's rows are kofin's to move and stamp."""

    def attached(self, row: Any, root: str) -> bool:
        """The library row points at the file and carries the tag and the
        badge; whether the row moved. The stamps are idempotent and go on
        every time, so a repair that wiped the links is healed by the
        startup reconcile."""
        from kofin.downloads import repoint

        moved = bool(repoint.repoint(row, root))
        repoint.stamp_tag(row)
        repoint.stamp_badge(row)
        return moved

    def detached(self, row: Any) -> None:
        from kofin.downloads import repoint

        repoint.unstamp_tag(row)
        repoint.clear_badge(row)

    def restore(self, row: Any, root: str) -> bool:
        from kofin.downloads import repoint

        return bool(repoint.restore(row, root))

    def mapped(self, item_id: str) -> bool:
        from kofin.downloads import repoint
        from kofin.sync.db import Database

        with Database("kofin") as opened:
            return repoint.mapping_for_on(opened.cursor, item_id) is not None

    def mark_watched(self, row: Any) -> None:
        """Kodi's own playcount, written straight through SQLite.

        Never JSON-RPC here: an announcer-visible library write feeds the
        userdata echo cycle (report, server echoes UserDataChanged, kofin
        writes it back), which terminates only because direct writes raise
        no Kodi announcement (service/kodiuserdata.py).
        """
        from kofin.downloads import repoint
        from kofin.sync.db import Database

        try:
            with Database("kofin") as kofin_db, Database("video") as video:
                mapping = repoint.mapping_for_on(kofin_db.cursor, row.jellyfin_id)
                if mapping is None:
                    return
                video.cursor.execute(
                    "UPDATE files SET playCount = 1, lastPlayed = ? WHERE idFile = ?",
                    (
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        mapping.kodi_fileid,
                    ),
                )
        except Exception:
            LOG.exception("could not mark %s watched locally", row.jellyfin_id)

    def watched_locally(self, row: Any) -> bool:
        from kofin.downloads import repoint
        from kofin.sync.db import Database

        with Database("kofin") as kofin_db, Database("video") as video:
            mapping = repoint.mapping_for_on(kofin_db.cursor, row.jellyfin_id)
            if mapping is None:
                return False
            video.cursor.execute(
                "SELECT playCount FROM files WHERE idFile = ?", (mapping.kodi_fileid,)
            )
            found = video.cursor.fetchone()
        return bool(found is not None and found[0])

    def last_touch(self, row: Any) -> Optional[Tuple[float, bool]]:
        """``(lastPlayed as unix seconds, has a resume point)`` off the file
        row the download was repointed onto; None without a library row.
        The resume point is a ``type = 1`` bookmark, Kodi's own RESUME kind."""
        from kofin.downloads import repoint
        from kofin.sync.db import Database

        with Database("kofin") as kofin_db, Database("video") as video:
            mapping = repoint.mapping_for_on(kofin_db.cursor, row.jellyfin_id)
            if mapping is None:
                return None
            video.cursor.execute(
                "SELECT lastPlayed FROM files WHERE idFile = ?", (mapping.kodi_fileid,)
            )
            found = video.cursor.fetchone()
            if found is None:
                return None
            video.cursor.execute(
                "SELECT 1 AS present FROM bookmark WHERE idFile = ? AND type = 1 LIMIT 1",
                (mapping.kodi_fileid,),
            )
            resuming = video.cursor.fetchone() is not None
        return _as_epoch(found[0]), resuming

    def music_view(self, force: bool) -> None:
        from kofin.sync import playlists
        from kofin.sync.nodes import music as music_nodes

        playlists.refresh_downloaded_music()
        music_nodes.write_music_nodes()

    def refresh_song_playlists(self, item_ids: Iterable[str]) -> None:
        from kofin.sync import playlists

        playlists.refresh_song_playlists_many(item_ids)


# Kodi's media type of a download row, for the readback by id.
_MEDIA = {"movie": "movie", "episode": "episode", "song": "song"}
_READ = {
    "movie": ("VideoLibrary.GetMovieDetails", "movieid", "moviedetails"),
    "episode": ("VideoLibrary.GetEpisodeDetails", "episodeid", "episodedetails"),
}


class NativeApi:
    """The API build: nothing of Kodi's moves; the catalogue's pass carries
    the badge and the tag, and the play state is read back by id."""

    @staticmethod
    def _store() -> Any:
        from kofin.sync.backends.api.identity import current_store

        return current_store()

    def attached(self, row: Any, root: str) -> bool:
        """The row keeps its plugin URL (the resolver plays the file); the
        pass is handed the item once, for the badge and the tag. Always
        true: the startup reconcile re-plans every done file, which is how
        a finish that died before this call still gets its badge."""
        self._replan(row)
        return True

    def detached(self, row: Any) -> None:
        self._replan(row)

    def restore(self, row: Any, root: str) -> bool:
        return True

    def _replan(self, row: Any) -> None:
        """Have the pass build the row again with its download state: the
        badge on a movie or an episode, the tag on a movie or on the show
        an episode belongs to."""
        items: List[str] = [row.jellyfin_id]
        if row.media_type == "episode" and row.series_id:
            items.append(row.series_id)
        try:
            self._store().invalidate(items)
        except Exception:
            LOG.exception(
                "download state of %s not handed to the pass", row.jellyfin_id
            )

    def mapped(self, item_id: str) -> bool:
        try:
            return self._store().entry(item_id) is not None
        except Exception:
            return False

    def mark_watched(self, row: Any) -> None:
        # The server is told (manager._push_played) and its answer reaches
        # Kodi's row through the pass, online now or when parked userdata is
        # replayed; a setter here would start the userdata echo cycle.
        return None

    def _row(self, row: Any, properties: List[str]) -> Optional[dict]:
        from kofin.core import kodirpc
        from kofin.sync.backends.api.identity import native_id_for

        read = _READ.get(row.media_type)
        if read is None:
            return None
        kodi_id = native_id_for(row.jellyfin_id, _MEDIA.get(row.media_type, "movie"))
        if kodi_id is None:
            return None
        method, id_param, result_key = read
        reply = kodirpc.call(method, {id_param: kodi_id, "properties": properties})
        if not isinstance(reply, dict):
            return None
        found = reply.get(result_key)
        return found if isinstance(found, dict) else None

    def watched_locally(self, row: Any) -> bool:
        found = self._row(row, ["playcount"])
        return bool(found and found.get("playcount"))

    def last_touch(self, row: Any) -> Optional[Tuple[float, bool]]:
        found = self._row(row, ["lastplayed", "resume"])
        if found is None:
            return None
        resume = found.get("resume") or {}
        return _as_epoch(found.get("lastplayed")), bool(resume.get("position"))

    def music_view(self, force: bool) -> None:
        # The Downloaded-music smart playlist is a file; the music node tree
        # filters on Kodi music sources this build never writes.
        from kofin.sync import playlists

        playlists.refresh_downloaded_music()

    def refresh_song_playlists(self, item_ids: Iterable[str]) -> None:
        # A song's playlist line is its plugin URL, which a download never moves.
        return None
