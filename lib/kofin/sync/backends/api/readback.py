"""Scoped readback of owned rows: one library's movies, one show's episodes.

A row is owned only when its namespaced unique id and its exact resolver URL
both say so. Readings are cached for the pass that made them and dropped
whenever a scan or a removal could have changed them.
"""

from typing import Any, Dict, Optional, Tuple

from . import paths
from .kinds import KINDS, PROPERTIES, rpc


class Readback:
    def __init__(self, key: str):
        self.key = key
        self._scopes: Dict[Tuple[Any, ...], Dict[Any, Dict[str, Any]]] = {}

    def clear(self):
        self._scopes.clear()

    @staticmethod
    def complete(reply, key):
        if not isinstance(reply, dict) or "limits" not in reply:
            raise RuntimeError("incomplete native readback")
        rows = reply.get(key, [])
        if len(rows) != reply["limits"]["total"]:
            raise RuntimeError("partial native readback")
        return rows

    def owns(self, kind, row, library, parent_id="") -> Optional[str]:
        """The item id of an owned row, or None."""
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

    def scope(self, kind, library="", parent_id="", tvshowid=None):
        """Owned rows of one scanner scope, cached for the pass.

        Seasons key by number and sets by title: neither carries an id of
        ours, and both exist only through the rows that reference them.
        """
        cache_key = (kind, library, parent_id)
        if cache_key in self._scopes:
            return self._scopes[cache_key]
        table = KINDS[kind]
        params: Dict[str, Any] = {"properties": PROPERTIES[kind]}
        if kind in ("Movie", "MusicVideo"):
            params["filter"] = {
                "field": "path",
                "operator": "startswith",
                "value": paths.library_dir(self.key, library, paths.CONTENT[kind]),
            }
        elif kind == "Series":
            params["filter"] = {
                "field": "path",
                "operator": "startswith",
                "value": paths.library_dir(self.key, library, "tvshows"),
            }
        elif kind in ("Episode", "Season"):
            if tvshowid is None:
                show = self.scope("Series", library).get(parent_id)
                if not show:
                    self._scopes[cache_key] = {}
                    return {}
                tvshowid = show["tvshowid"]
            params["tvshowid"] = tvshowid
        rows = self.complete(rpc(table.listing, params), table.list_key)
        result: Dict[Any, Dict[str, Any]] = {}
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

    def forget(self, kind, library="", parent_id=""):
        self._scopes.pop((kind, library, parent_id), None)
        if kind == "Series":
            # A show's episodes and seasons are read through its row.
            for key in [k for k in self._scopes if k[0] in ("Episode", "Season")]:
                self._scopes.pop(key, None)

    def remember(self, kind, library, parent_id, cache_key, row):
        """Put a freshly confirmed row into an existing cached scope."""
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
