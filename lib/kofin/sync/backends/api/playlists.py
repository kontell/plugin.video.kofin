"""Jellyfin playlists as Kodi playlist files, resolved through the catalogue.

The SQL build resolves a playlist's entries through its mapping table and
Kodi's databases. This build resolves them through the store: an entry is an
item the pass has acknowledged, its line the resolver URL Kodi filed the row
under (a song's file in its album directory, a video's query URL) and its
label the server's own playlist listing. Order and duplicates are the
server's; the managed folders, the file naming, the state tables and the
prune rules are the shared module's (sync/playlists.py).
"""

from typing import Any, Dict, Optional, Set

from kofin.core.log import Logger
from kofin.sync import dynamic, kofindb, playlists, private
from . import paths
from .store import Store

LOG = Logger(__name__)

VIDEO_KINDS = ("Movie", "Episode", "MusicVideo")
# The one switch for the generated presentation, the node tree included.
SETTING = dynamic.SETTING


class Resolver:
    def __init__(self, store: Store):
        self.store = store
        self.key = store.namespace

    def entry(self, item: Dict[str, Any], side: str) -> Optional[playlists.Entry]:
        """The line for one playlist item, or None when Kodi has no row of it:
        an item outside the selection, or one the pass has not filed yet."""
        item_id = str(item.get("Id") or "")
        entry = self.store.entry(item_id) if item_id else None
        if entry is None:
            return None
        if side == "Audio" and entry.kind != "Audio":
            return None
        if side == "Video" and entry.kind not in VIDEO_KINDS:
            return None
        mapping = self.store.mapping(item_id)
        if mapping is None or mapping.kodi_id is None:
            return None
        if side == "Audio":
            payload = self.store.item(item_id) or {}
            return playlists.Entry(
                path=paths.playback_url(
                    self.key,
                    "Audio",
                    entry.library,
                    item_id,
                    entry.parent_id,
                    paths.container_of(payload),
                ),
                title=str(item.get("Name") or payload.get("Name") or ""),
                artist=" / ".join(str(a) for a in (item.get("Artists") or [])),
                track=int(item.get("IndexNumber") or 0) & 0xFFFF,
                duration=int((item.get("RunTimeTicks") or 0) // 10_000_000),
            )
        return playlists.Entry(
            path=paths.playback_url(
                self.key, entry.kind, entry.library, item_id, entry.parent_id
            ),
            title=str(item.get("Name") or ""),
        )


def wanted() -> bool:
    return dynamic.wanted()


def enabled_kinds() -> Set[str]:
    """Audio and Video sides, from the kinds of the synced libraries."""
    whitelist = {
        x.replace("Mixed:", "") for x in (private.get_sync().get("Whitelist") or [])
    }
    with private.Database() as db:
        views = [
            {"Id": view_id, "media_type": media}
            for view_id, _name, media in db.cursor.execute(
                "SELECT view_id, view_name, media_type FROM view"
            ).fetchall()
        ]
    return playlists.enabled_kinds(views, whitelist)


def _drop_side(side: str, state, root) -> int:
    """The managed files and the states of a side whose last library left
    the selection, the way the whole feature's go when it is turned off."""
    stale = [row for row in state.get_playlist_states() if row[1] == side]
    if not stale:
        return 0
    if side == "Audio":
        removed = playlists.cleanup_managed_playlists(root)
    else:
        removed = playlists.cleanup_video_playlists(root)
    for row in stale:
        state.remove_playlist_state(row[0])
    LOG.info("%s playlists pruned: no synced library left (%d files)", side, removed)
    return removed


def reconcile(api, store: Store, music_root=None, video_root=None) -> Dict[str, int]:
    """Every server playlist of the enabled sides, written and pruned; a
    side with no synced library left loses its files and states."""
    if not wanted():
        return {}
    kinds = enabled_kinds()
    with private.Database() as db:
        state = kofindb.JellyfinDatabase(db.cursor)
        pruned = 0
        if "Audio" not in kinds:
            pruned += _drop_side("Audio", state, music_root)
        if "Video" not in kinds:
            pruned += _drop_side("Video", state, video_root)
        if not kinds:
            return {"pruned": pruned} if pruned else {}
        return playlists.reconcile(
            api,
            None,
            None,
            None,
            kofindb.JellyfinDatabase(db.cursor),
            kinds,
            music_root=music_root,
            video_root=video_root,
            resolver=Resolver(store),
        )


def apply(
    api, store: Store, playlist_id: str, music_root=None, video_root=None
) -> bool:
    """One server playlist, written or pruned."""
    if not wanted() or not playlist_id:
        return False
    kinds = enabled_kinds()
    if not kinds:
        return False
    item = api.item(playlist_id)
    if not isinstance(item, dict) or not item.get("Id"):
        return False
    with private.Database() as db:
        return playlists.apply_one(
            api,
            None,
            None,
            None,
            kofindb.JellyfinDatabase(db.cursor),
            item,
            kinds,
            music_root=music_root,
            video_root=video_root,
            resolver=Resolver(store),
        )


def cleanup() -> None:
    """The managed playlists go, both sides; the state with them."""
    playlists.cleanup_managed_playlists()
    playlists.cleanup_video_playlists()
    with private.Database() as db:
        kofindb.JellyfinDatabase(db.cursor).remove_playlist_states()
