"""A Kodi video library stand-in for the public-API backend's unit tests.

It answers the JSON-RPC methods the backend uses, imports rows from the
store the way the scanner would -- from the pinned committed generation, per
scanner directory -- and keeps the few rules the backend relies on: a direct
scan of a show folder only adds episodes to a show that already exists,
``clearmode: "remove"`` deletes everything filed under the path, a refresh
replaces the row and its id, and ``Clean`` drops sets no movie links.
"""

import copy
import re

from kofin.sync.backends.api import metadata, paths

SERVER = "http://fixture.invalid"


class Kodi:
    def __init__(self, store):
        self.store = store
        self.key = store.namespace
        self.rows = {kind: {} for kind in metadata.MEDIATYPE}
        self.bindings = {}
        self.next_id = 1
        self.calls = []
        self.fail = ""
        self.accept_without_apply = False
        self.monitor = None
        self.scanned = []

    # -- helpers ---------------------------------------------------------------

    def _id(self):
        self.next_id += 1
        return self.next_id - 1

    def owned(self, kind):
        """item id -> row for every row this store owns, by unique id."""
        prefix = self.key + ":"
        result = {}
        for row in self.rows[kind].values():
            uid = (row.get("uniqueid") or {}).get("kofin", "")
            if uid.startswith(prefix):
                result[uid[len(prefix) :]] = row
        return result

    def _collections(self):
        return metadata.collections_of(
            r.item for r in self.store.records(kind="BoxSet").values()
        )

    def imported(self, record, seasons=(), set_name=None):
        kind = record.kind
        row = metadata.details(
            record.item, SERVER, self.key, record.library, " / ", seasons, set_name
        )
        row[METHODS[kind][0]] = self._id()
        if kind == "Movie":
            row.setdefault("set", "")
        if kind == "Series":
            row["file"] = paths.show_dir(self.key, record.library, record.item_id)
        else:
            row["file"] = paths.playback_url(
                self.key, kind, record.library, record.item_id, record.parent_id
            )
        if set_name:
            self._set_row(set_name)
        return row

    def _set_row(self, title):
        for row in self.rows["BoxSet"].values():
            if row["title"] == title:
                return row
        row = {"setid": self._id(), "title": title, "plot": "", "art": {}}
        self.rows["BoxSet"][row["setid"]] = row
        return row

    def _season_row(self, tvshowid, number, name=""):
        for row in self.rows["Season"].values():
            if row["tvshowid"] == tvshowid and row["season"] == number:
                return row
        row = {
            "seasonid": self._id(),
            "season": number,
            "title": name or "Season %d" % number,
            "art": {},
            "tvshowid": tvshowid,
        }
        self.rows["Season"][row["seasonid"]] = row
        return row

    def _import_show(self, record):
        seasons = [
            r.item
            for r in self.store.records(
                kind="Season", parent_id=record.item_id
            ).values()
        ]
        row = self.imported(record, seasons)
        self.rows["Series"][row["tvshowid"]] = row
        for season in seasons:
            if season.get("IndexNumber") is not None:
                self._season_row(
                    row["tvshowid"],
                    int(season["IndexNumber"]),
                    metadata.season_title(season),
                )
        return row

    def _import_episodes(self, show_row, library, series_id):
        present = self.owned("Episode")
        for item_id, record in sorted(
            self.store.records(kind="Episode", parent_id=series_id).items()
        ):
            if item_id in present or record.library != library:
                continue
            numbers = metadata.episode_numbers(record.item)
            if numbers is None:
                continue
            row = self.imported(record)
            row["tvshowid"] = show_row["tvshowid"]
            row["seasonid"] = self._season_row(show_row["tvshowid"], numbers["season"])[
                "seasonid"
            ]
            self.rows["Episode"][row["episodeid"]] = row

    def scan(self, directory):
        self.scanned.append(directory)
        location = paths.parse(directory)
        assert location is not None and location.library, directory
        library = location.library
        if location.content in ("movies", "musicvideos"):
            kind = "Movie" if location.content == "movies" else "MusicVideo"
            present = self.owned(kind)
            collections = self._collections() if kind == "Movie" else {}
            for item_id, record in sorted(
                self.store.records(kind=kind, library=library).items()
            ):
                if item_id not in present:
                    row = self.imported(record, set_name=collections.get(item_id))
                    self.rows[kind][row[METHODS[kind][0]]] = row
        elif location.series is None:
            shows = self.owned("Series")
            for series_id, record in sorted(
                self.store.records(kind="Series", library=library).items()
            ):
                row = shows.get(series_id)
                if row is None:
                    # Kodi finds a plugin folder's scraper only through the
                    # folder's own binding: the parent of a plugin path is
                    # the plugin root (URIUtils::GetParentPath).
                    if (
                        paths.show_dir(self.key, library, series_id)
                        not in self.bindings
                    ):
                        continue
                    row = self._import_show(record)
                self._import_episodes(row, library, series_id)
        else:
            # Kodi lists a directly scanned show folder itself, with no tag:
            # only a show that already exists gains episodes.
            row = self.owned("Series").get(location.series)
            if row is not None:
                self._import_episodes(row, library, location.series)
        self.monitor.finished += 1

    def remove_content(self, path):
        for kind in ("Movie", "MusicVideo", "Episode"):
            self.rows[kind] = {
                k: r
                for k, r in self.rows[kind].items()
                if not r["file"].startswith(path)
            }
        gone = [k for k, r in self.rows["Series"].items() if r["file"].startswith(path)]
        for tvshowid in gone:
            self._remove_show(tvshowid)
        self.bindings = {
            p: c for p, c in self.bindings.items() if not p.startswith(path)
        }

    def _remove_show(self, tvshowid):
        self.rows["Series"].pop(tvshowid, None)
        for kind in ("Episode", "Season"):
            self.rows[kind] = {
                k: r
                for k, r in self.rows[kind].items()
                if r.get("tvshowid") != tvshowid
            }

    def _refresh(self, kind, kodi_id, episodes=False):
        old = self.rows[kind].pop(kodi_id)
        item_id = old["uniqueid"]["kofin"].split(":")[1]
        record = self.store.records(kind=kind, item_ids=[item_id]).get(item_id)
        if record is None:
            return
        if kind == "Series":
            if episodes:
                self._remove_show(kodi_id)
                row = self._import_show(record)
                self._import_episodes(row, record.library, item_id)
            else:
                row = self._import_show(record)
                for other in list(self.rows["Episode"].values()) + list(
                    self.rows["Season"].values()
                ):
                    if other.get("tvshowid") == kodi_id:
                        other["tvshowid"] = row["tvshowid"]
            return
        row = self.imported(
            record,
            set_name=self._collections().get(item_id) if kind == "Movie" else None,
        )
        if kind == "Episode":
            row["tvshowid"] = old["tvshowid"]
            row["seasonid"] = old["seasonid"]
        self.rows[kind][row[METHODS[kind][0]]] = row

    # -- the JSON-RPC surface ------------------------------------------------------------

    def batch(self, requests):
        results = []
        for method, params in requests:
            try:
                results.append(self.rpc(method, params))
            except RuntimeError as error:
                results.append(error)
        return results

    def rpc(self, method, params=None):
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method == self.fail:
            raise RuntimeError("injected native failure")
        match = re.fullmatch(
            r"(Video|Audio)Library\.(Get|Set|Refresh|Remove)(\w+?)(Details)?", method
        )
        if method == "VideoLibrary.SetSourceContent":
            if params["content"] == "none":
                if (
                    params.get("clearmode") == "remove"
                    and not self.accept_without_apply
                ):
                    self.remove_content(params["path"])
                self.bindings.pop(params["path"], None)
            else:
                self.bindings[params["path"]] = params["content"]
            return "OK"
        if method == "VideoLibrary.Scan":
            self.scan(params["directory"])
            return "OK"
        if method == "VideoLibrary.Clean":
            linked = {r.get("set") for r in self.rows["Movie"].values()}
            self.rows["BoxSet"] = {
                k: r for k, r in self.rows["BoxSet"].items() if r["title"] in linked
            }
            return "OK"
        if not match:
            raise RuntimeError("Kodi refused " + method)
        library, verb, noun, details = match.groups()
        if library == "Audio":
            return {"limits": {"total": 0}}
        kind = KIND_BY_NOUN.get(noun)
        if kind is None:
            raise RuntimeError("Kodi refused " + method)
        id_param, result_key, list_key = METHODS[kind]
        if verb == "Get" and not details:
            rows = list(self.rows[kind].values())
            if kind == "BoxSet":
                # GetSetsByWhere groups movie_view by set: a set no movie
                # links is not listed, even though its row still exists.
                linked = {r.get("set") for r in self.rows["Movie"].values()}
                rows = [r for r in rows if r["title"] in linked]
            if kind == "Season":
                # season_view joins episodes: an empty season is invisible.
                rows = [
                    r
                    for r in rows
                    if any(
                        e.get("tvshowid") == r["tvshowid"]
                        and e.get("season") == r["season"]
                        for e in self.rows["Episode"].values()
                    )
                ]
            if "filter" in params:
                prefix = params["filter"]["value"]
                rows = [r for r in rows if r["file"].startswith(prefix)]
            if "tvshowid" in params:
                rows = [r for r in rows if r.get("tvshowid") == params["tvshowid"]]
            return {list_key: copy.deepcopy(rows), "limits": {"total": len(rows)}}
        kodi_id = params[id_param]
        if kodi_id not in self.rows[kind]:
            raise RuntimeError("Kodi refused " + method)
        if verb == "Get":
            return {result_key: copy.deepcopy(self.rows[kind][kodi_id])}
        if verb == "Set":
            if not self.accept_without_apply:
                row = self.rows[kind][kodi_id]
                values = {k: v for k, v in params.items() if k != id_param}
                for field in ("art", "ratings", "uniqueid"):
                    if isinstance(values.get(field), dict):
                        merged = dict(row.get(field) or {})
                        for name, value in values[field].items():
                            if value is None:
                                merged.pop(name, None)
                            else:
                                merged[name] = value
                        values[field] = merged
                if kind == "Movie" and values.get("set"):
                    self._set_row(values["set"])
                if kind == "Season" and values.get("title") == "":
                    values["title"] = "Season %d" % row["season"]
                row.update(copy.deepcopy(values))
            return "OK"
        if verb == "Refresh":
            self._refresh(kind, kodi_id, params.get("refreshepisodes", False))
            return "OK"
        if verb == "Remove":
            if not self.accept_without_apply:
                if kind == "Series":
                    self._remove_show(kodi_id)
                else:
                    del self.rows[kind][kodi_id]
            return "OK"
        raise RuntimeError("Kodi refused " + method)


KIND_BY_NOUN = {
    "Movies": "Movie",
    "Movie": "Movie",
    "TVShows": "Series",
    "TVShow": "Series",
    "Seasons": "Season",
    "Season": "Season",
    "Episodes": "Episode",
    "Episode": "Episode",
    "MusicVideos": "MusicVideo",
    "MusicVideo": "MusicVideo",
    "MovieSets": "BoxSet",
    "MovieSet": "BoxSet",
}
# kind -> (id parameter, details key, list key)
METHODS = {
    "Movie": ("movieid", "moviedetails", "movies"),
    "Series": ("tvshowid", "tvshowdetails", "tvshows"),
    "Season": ("seasonid", "seasondetails", "seasons"),
    "Episode": ("episodeid", "episodedetails", "episodes"),
    "MusicVideo": ("musicvideoid", "musicvideodetails", "musicvideos"),
    "BoxSet": ("setid", "setdetails", "sets"),
}
