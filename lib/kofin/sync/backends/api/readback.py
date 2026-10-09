"""Scoped readback of owned rows: one library's movies, one show's episodes,
one library's songs.

A video row is owned only when its namespaced unique id and its exact
resolver URL both say so. A song has no unique id -- Kodi keeps none for
music -- so its ownership is its URL alone: the directory it is filed under
is ours and the query names the item. Readings are cached for the pass that
made them and dropped whenever a scan or a removal could have changed them.

A library's songs are read in one listing (paged, 5,000 to a page) and split
by directory here: the pass asks for one directory at a time, 1,500 times,
and must not pay a call each time.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from kofin.core.log import Logger
from . import paths
from .kinds import KINDS, PROPERTIES, rpc

LOG = Logger(__name__)

PAGE = 5000


def artist_key(name) -> str:
    return (name or "").strip(" \t\n\r\v\f").casefold()


class Readback:
    def __init__(self, key: str):
        self.key = key
        self._scopes: Dict[Tuple[Any, ...], Dict[Any, Dict[str, Any]]] = {}
        # library -> directory key -> item id -> row
        self._folders: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}

    def clear(self):
        self._scopes.clear()
        self._folders.clear()

    @staticmethod
    def complete(reply, key):
        if not isinstance(reply, dict) or "limits" not in reply:
            raise RuntimeError("incomplete native readback")
        rows = reply.get(key, [])
        if len(rows) != reply["limits"]["total"]:
            raise RuntimeError("partial native readback")
        return rows

    def _paged(self, table, params) -> List[Dict[str, Any]]:
        """A complete listing in pages; the total must hold across them."""
        began = time.monotonic()
        rows: List[Dict[str, Any]] = []
        total: Optional[int] = None
        while True:
            page = dict(params, limits={"start": len(rows), "end": len(rows) + PAGE})
            reply = rpc(table.listing, page)
            if not isinstance(reply, dict) or not isinstance(
                reply.get("limits", {}).get("total"), int
            ):
                raise RuntimeError("incomplete native readback")
            count = reply["limits"]["total"]
            if total is not None and count != total:
                raise RuntimeError("native listing changed during readback")
            total = count
            chunk = reply.get(table.list_key, [])
            rows.extend(chunk)
            if len(rows) >= total:
                break
            if not chunk:
                raise RuntimeError("partial native readback")
        if len(rows) != total:
            raise RuntimeError("partial native readback")
        if total >= PAGE:
            LOG.info(
                "read back %d %s in %.1f s",
                total,
                table.list_key,
                time.monotonic() - began,
            )
        return rows

    def _path_filter(self, prefix):
        return {"field": "path", "operator": "startswith", "value": prefix}

    # -- ownership -----------------------------------------------------------

    def owns(self, kind, row, library, parent_id="") -> Optional[str]:
        """The item id of an owned row, or None."""
        if kind == "Audio":
            location, item_id = paths.parse_item(row.get("file") or "")
            if (
                location is None
                or location.key != self.key
                or location.library != library
                or location.content != "music"
                or not location.folder
                or not item_id
            ):
                return None
            if parent_id and location.folder != parent_id:
                return None
            return item_id
        if kind in ("MusicAlbum", "MusicArtist"):
            return None
        uid = str((row.get("uniqueid") or {}).get("kofin", ""))
        if not uid.startswith(self.key + ":"):
            return None
        item_id = uid[len(self.key) + 1 :]
        if kind == "Series":
            expected = paths.show_dir(self.key, library, item_id).rstrip("/")
            if (row.get("file") or "").rstrip("/") != expected:
                return None
        elif row.get("file") != paths.playback_url(
            self.key, kind, library, item_id, parent_id
        ):
            return None
        return item_id

    # -- scopes --------------------------------------------------------------

    def scope(self, kind, library="", parent_id="", tvshowid=None):
        """Owned rows of one scanner scope, cached for the pass.

        Seasons key by number and sets by title: neither carries an id of
        ours, and both exist only through the rows that reference them.
        Albums key by Kodi's album id, found through the songs' ``albumid``;
        artists by name, which is all Kodi matches them by.
        """
        if kind == "Audio":
            songs = self._songs(library)
            if not parent_id:
                return songs
            return self._folders[library].get(parent_id, {})
        cache_key = (kind, library, parent_id)
        if cache_key in self._scopes:
            return self._scopes[cache_key]
        table = KINDS[kind]
        params: Dict[str, Any] = {"properties": PROPERTIES[kind]}
        if kind in ("Movie", "MusicVideo"):
            params["filter"] = self._path_filter(
                paths.library_dir(self.key, library, paths.CONTENT[kind])
            )
        elif kind == "Series":
            params["filter"] = self._path_filter(
                paths.library_dir(self.key, library, "tvshows")
            )
        elif kind in ("Episode", "Season"):
            if tvshowid is None:
                show = self.scope("Series", library).get(parent_id)
                if not show:
                    self._scopes[cache_key] = {}
                    return {}
                tvshowid = show["tvshowid"]
            params["tvshowid"] = tvshowid
        elif kind in ("MusicAlbum", "MusicArtist"):
            params["filter"] = self._path_filter(
                paths.library_dir(self.key, library, "music")
            )
            if kind == "MusicArtist":
                params["albumartistsonly"] = False
            rows = self._paged(table, params)
            result: Dict[Any, Any] = {}
            for row in rows:
                if kind == "MusicAlbum":
                    result[row[table.id_param]] = row
                    continue
                name = artist_key(row.get("artist") or row.get("label"))
                # Two artists of one name: neither can be told apart by
                # anything the API lists, so neither is claimed.
                result[name] = None if name in result else row
            self._scopes[cache_key] = result
            return result
        rows = self.complete(rpc(table.listing, params), table.list_key)
        result = {}
        for row in rows:
            if kind == "Season":
                result[int(row.get("season", -1))] = row
                continue
            if kind == "BoxSet":
                result[row.get("title", "")] = row
                continue
            item_id = self.owns(kind, row, library, parent_id)
            if item_id is None:
                continue
            if item_id in result:
                raise RuntimeError("duplicate owned %s identity" % kind)
            result[item_id] = row
        self._scopes[cache_key] = result
        return result

    def _songs(self, library):
        cache_key = ("Audio", library, "")
        if cache_key in self._scopes:
            return self._scopes[cache_key]
        table = KINDS["Audio"]
        rows = self._paged(
            table,
            {
                "properties": PROPERTIES["Audio"],
                "filter": self._path_filter(
                    paths.library_dir(self.key, library, "music")
                ),
                "sort": {"method": "file", "order": "ascending"},
            },
        )
        result: Dict[str, Dict[str, Any]] = {}
        folders: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in rows:
            location, item_id = paths.parse_item(row.get("file") or "")
            if (
                location is None
                or location.key != self.key
                or location.library != library
                or not location.folder
                or not item_id
            ):
                continue
            # A song moved between albums has a row in each directory until
            # the old one is rescanned; the directory view keeps both, the
            # flat view is for presence only.
            result[item_id] = row
            folders.setdefault(location.folder, {})[item_id] = row
        self._scopes[cache_key] = result
        self._folders[library] = folders
        return result

    def folders(self, library) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Every directory of the library's songs Kodi holds, with its rows."""
        self._songs(library)
        return self._folders[library]

    def directory(self, library, folder) -> Dict[str, Dict[str, Any]]:
        """One music directory's owned rows, read now and not cached: the
        confirmation after a scan, and the one lookup a playback needs."""
        rows = self._paged(
            KINDS["Audio"],
            {
                "properties": PROPERTIES["Audio"],
                "filter": self._path_filter(paths.music_dir(self.key, library, folder)),
            },
        )
        result = {}
        for row in rows:
            item_id = self.owns("Audio", row, library, folder)
            if item_id is not None:
                result[item_id] = row
        return result

    def forget_music(self, library):
        """Drop every music scope of a library: a scan or a patch changed it."""
        for kind in ("Audio", "MusicAlbum", "MusicArtist"):
            self.forget(kind, library)

    def forget(self, kind, library="", parent_id=""):
        if kind == "Audio":
            self._scopes.pop(("Audio", library, ""), None)
            self._folders.pop(library, None)
            return
        self._scopes.pop((kind, library, parent_id), None)
        if kind == "Series":
            # A show's episodes and seasons are read through its row.
            for key in [k for k in self._scopes if k[0] in ("Episode", "Season")]:
                self._scopes.pop(key, None)

    def remember(self, kind, library, parent_id, cache_key, row):
        """Put a freshly confirmed row into an existing cached scope."""
        if kind == "Audio":
            if ("Audio", library, "") in self._scopes:
                self._scopes[("Audio", library, "")][cache_key] = row
                self._folders[library].setdefault(parent_id, {})[cache_key] = row
            return
        scope_key = (kind, library, parent_id)
        if scope_key in self._scopes:
            self._scopes[scope_key][cache_key] = row

    def details(self, kind, kodi_id):
        table = KINDS[kind]
        return rpc(
            table.getter, {table.id_param: kodi_id, "properties": PROPERTIES[kind]}
        ).get(table.result_key, {})

    def owned(self, kind, kodi_id, item_id, library, parent_id=""):
        row = self.details(kind, kodi_id)
        if self.owns(kind, row, library, parent_id) != item_id:
            raise RuntimeError("%s ownership changed" % kind)
        return row
