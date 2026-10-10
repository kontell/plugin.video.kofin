"""A Kodi library stand-in for the public-API backend's unit tests.

It answers the JSON-RPC methods the backend uses, imports rows from the
store the way the scanner would -- from the pinned committed generation, per
scanner directory -- and keeps the few rules the backend relies on: a direct
scan of a show folder only adds episodes to a show that already exists,
``clearmode: "remove"`` deletes everything filed under the path, a refresh
replaces the row and its id, and ``Clean`` drops sets no movie links.

The music side keeps the music scanner's rules: a scan of a directory
replaces that directory's songs with the listing, a song already on the path
keeps its id, play count and last played, a new song's play count is zero
whatever its tag says, albums and artists are derived from the songs' tags
and vanish with their last song, no art is taken from a plugin listing, and
a scan of a root walks every folder the root lists.
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
        self.recursive = {}
        self.next_id = 1
        self.calls = []
        self.fail = ""
        self.accept_without_apply = False
        self.monitor = None
        self.scanned = []
        # Music: songs by Kodi id; albums and artists derived from them.
        self.songs = {}
        self.albums = {}
        self.artists = {}
        self.music_scanned = []
        self.holding = False
        # A directory whose listing the fake should take from here instead
        # of the store (a failed or foreign listing), or drop entirely.
        self.listings = {}
        # Movie versions and extras: Kodi's settings, the folders the pass
        # had Kodi list (its directory cache), each binding's folder-names
        # flag, the extras filed on a movie row, and the extras folders a
        # scan turned into phantom discs instead.
        self.settings = {"videolibrary.ignorevideoextras": True}
        self.listed = []
        self.dirnames = {}
        self.extras = {}
        self.phantoms = []

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
            wanted = None
            if kind == "Movie" and location.movie:
                # A movie folder scanned by name imports its one movie, if
                # the folder carries a binding of its own (the root lists
                # every movie as a file, so a root scan needs none).
                wanted = (
                    [location.movie]
                    if paths.movie_dir(self.key, library, location.movie)
                    in self.bindings
                    else []
                )
            for item_id, record in sorted(
                self.store.records(kind=kind, library=library).items()
            ):
                if wanted is not None and item_id not in wanted:
                    continue
                if item_id not in present:
                    row = self.imported(record, set_name=collections.get(item_id))
                    self.rows[kind][row[METHODS[kind][0]]] = row
                if kind == "Movie" and location.movie:
                    self._scan_movie_assets(record)
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

    def _scan_movie_assets(self, record):
        """A movie folder scanned by name. Kodi imports each further version
        file as a movie of its own (the plugin tag path never groups), and
        reads the extras folder only when the folder's binding uses folder
        names, "Ignore video extras" is off and its directory cache knows the
        folder and the disc folders probed below it; otherwise the folder is
        a phantom disc and the extras are lost."""
        folder = paths.movie_dir(self.key, record.library, record.item_id)
        canonical = paths.playback_url(
            self.key, "Movie", record.library, record.item_id
        )
        files = {r["file"] for r in self.rows["Movie"].values()}
        for source in metadata.version_sources(record.item)[1:]:
            url = paths.version_url(
                self.key, record.library, record.item_id, source["Id"]
            )
            if url in files:
                continue
            row = self.imported(
                record, set_name=self._collections().get(record.item_id)
            )
            row["file"] = url
            self.rows["Movie"][row["movieid"]] = row
        features = metadata.special_features(record.item)
        owner = next(
            (r for r in self.rows["Movie"].values() if r["file"] == canonical), None
        )
        if not features or owner is None:
            return
        extras = folder + paths.EXTRAS
        probes = {extras, extras + "VIDEO_TS/", extras + "BDMV/"}
        if (
            self.settings.get("videolibrary.ignorevideoextras")
            or not self.dirnames.get(folder)
            or not probes <= set(self.listed)
        ):
            self.phantoms.append(folder)
            return
        self.extras[owner["movieid"]] = [paths.extra_name(f) for f in features]

    def remove_content(self, path):
        for kind in ("Movie", "MusicVideo", "Episode"):
            self.rows[kind] = {
                k: r
                for k, r in self.rows[kind].items()
                if not r["file"].startswith(path)
            }
        self.extras = {k: v for k, v in self.extras.items() if k in self.rows["Movie"]}
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

    # -- music -----------------------------------------------------------------

    def music_scan(self, directory):
        location = paths.parse(directory)
        if location is not None and location.hold:
            # The hold listing stays open until the pass releases it.
            self.holding = True
            return
        self.music_scanned.append(directory)
        assert location is not None and location.content == "music", directory
        if location.folder is None:
            for folder in self._root_folders(location.library):
                self._scan_folder(location.library, folder)
        else:
            self._scan_folder(location.library, location.folder)
        self._cleanup_music()
        self.monitor.music_finished += 1

    def _root_folders(self, library):
        """What the provider's root listing shows: live folders and the
        folders of songs with a pending removal."""
        folders = set(self.store.folders("Audio", library))
        folders.update(
            e.parent_id
            for e in self.store.tombstones().values()
            if e.kind == "Audio" and e.library == library
        )
        return sorted(folders)

    def _listing(self, library, folder):
        """(url, tags) pairs the provider would hand the scanner."""
        directory = paths.music_dir(self.key, library, folder)
        if directory in self.listings:
            return self.listings[directory]
        album = None
        if not folder.startswith(paths.SINGLES):
            found = self.store.records(kind="MusicAlbum", item_ids=[folder])
            album = found[folder].item if folder in found else None
        rows = []
        for item_id, record in sorted(
            self.store.records(kind="Audio", library=library, parent_id=folder).items()
        ):
            rows.append(
                (
                    paths.song_url(
                        self.key,
                        library,
                        folder,
                        item_id,
                        paths.container_of(record.item),
                    ),
                    metadata.song_tags(record.item, album),
                )
            )
        return rows

    def _scan_folder(self, library, folder):
        directory = paths.music_dir(self.key, library, folder)
        listed = self._listing(library, folder)
        # RemoveSongsFromPath, exact: this directory's songs go, remembered
        # by file name for id and userdata reuse.
        previous = {
            row["file"]: row
            for row in self.songs.values()
            if row["file"].startswith(directory)
        }
        for row in list(previous.values()):
            del self.songs[row["songid"]]
        for url, tags in listed:
            old = previous.get(url)
            album = self._album_row(tags, directory)
            row = {
                "songid": old["songid"] if old else self._id(),
                "file": url,
                "label": tags["title"],
                "title": tags["title"],
                "albumid": album["albumid"],
                "album": tags["album"],
                "artist": list(tags["artist"]),
                "albumartist": list(tags["albumartist"]),
                "genre": list(tags["genre"]),
                "track": tags["track"],
                "disc": tags["disc"],
                "duration": tags["duration"],
                "year": tags["year"],
                "musicbrainztrackid": tags["musicbrainztrackid"],
                # CSong zeroes a new song's play count; a rescan keeps the
                # database's.
                "playcount": old["playcount"] if old else 0,
                "lastplayed": old["lastplayed"] if old else "",
                "userrating": 0,
            }
            self.songs[row["songid"]] = row
            for name in tags["artist"] or tags["albumartist"]:
                self._artist_row(name)
            for name in tags["albumartist"]:
                self._artist_row(name)

    def _album_row(self, tags, directory):
        """Kodi matches an album by MusicBrainz id, else title and album
        artist; the first directory to add it sets its path."""
        key = (
            tags["musicbrainzalbumid"] or None,
            tags["album"].casefold(),
            tuple(n.casefold() for n in tags["albumartist"]),
        )
        for row in self.albums.values():
            if row["_key"] == key or (key[0] and row["_key"][0] == key[0]):
                return row
        row = {
            "albumid": self._id(),
            "label": tags["album"],
            "title": tags["album"],
            "artist": list(tags["albumartist"]),
            "art": {},
            "description": "",
            "_key": key,
            "_path": directory,
        }
        self.albums[row["albumid"]] = row
        return row

    def _artist_row(self, name):
        key = name.casefold()
        for row in self.artists.values():
            if row["artist"].casefold() == key:
                return row
        row = {
            "artistid": self._id(),
            "artist": name,
            "label": name,
            "art": {},
            "description": "",
        }
        self.artists[row["artistid"]] = row
        return row

    def _cleanup_music(self):
        """CleanupOrphanedItems: albums without songs, artists without either."""
        used_albums = {row["albumid"] for row in self.songs.values()}
        self.albums = {k: r for k, r in self.albums.items() if k in used_albums}
        credited = set()
        for row in self.songs.values():
            credited.update(n.casefold() for n in row["artist"])
            credited.update(n.casefold() for n in row["albumartist"])
        self.artists = {
            k: r for k, r in self.artists.items() if r["artist"].casefold() in credited
        }

    def _music_rows(self, kind, params):
        rows = {
            "Audio": list(self.songs.values()),
            "MusicAlbum": list(self.albums.values()),
            "MusicArtist": list(self.artists.values()),
        }[kind]
        if "filter" in params:
            prefix = params["filter"]["value"]
            if kind == "Audio":
                rows = [r for r in rows if r["file"].startswith(prefix)]
            else:
                albums = {
                    r["albumid"]
                    for r in self.songs.values()
                    if r["file"].startswith(prefix)
                }
                if kind == "MusicAlbum":
                    rows = [r for r in rows if r["albumid"] in albums]
                else:
                    names = set()
                    for song in self.songs.values():
                        if song["file"].startswith(prefix):
                            names.update(n.casefold() for n in song["artist"])
                            names.update(n.casefold() for n in song["albumartist"])
                    rows = [r for r in rows if r["artist"].casefold() in names]
        if (params.get("sort") or {}).get("method") == "file":
            rows = sorted(rows, key=lambda r: r["file"])
        limits = params.get("limits") or {}
        start = limits.get("start", 0)
        end = limits.get("end", len(rows))
        page = rows[start:end] if end >= 0 else rows[start:]
        return page, len(rows)

    def _music_properties(self, row, params):
        wanted = set(params.get("properties") or []) | {"label"}
        return {
            k: copy.deepcopy(v)
            for k, v in row.items()
            if k in wanted or k.endswith("id") and not k.startswith("_")
        }

    def music_rpc(self, method, params):
        if method == "AudioLibrary.Scan":
            self.music_scan(params["directory"])
            return "OK"
        match = re.fullmatch(
            r"AudioLibrary\.(Get|Set)(Songs?|Albums?|Artists?)(Details)?", method
        )
        if not match:
            raise RuntimeError("Kodi refused " + method)
        verb, noun, details = match.groups()
        kind = {"Song": "Audio", "Album": "MusicAlbum", "Artist": "MusicArtist"}[
            noun.rstrip("s")
        ]
        id_param, result_key, list_key = METHODS[kind]
        if verb == "Get" and not details:
            page, total = self._music_rows(kind, params)
            return {
                list_key: [self._music_properties(r, params) for r in page],
                "limits": {"start": 0, "end": len(page), "total": total},
            }
        table = {
            "Audio": self.songs,
            "MusicAlbum": self.albums,
            "MusicArtist": self.artists,
        }[kind]
        kodi_id = params[id_param]
        if kodi_id not in table:
            raise RuntimeError("Kodi refused " + method)
        if verb == "Get":
            return {result_key: self._music_properties(table[kodi_id], params)}
        if not self.accept_without_apply:
            row = table[kodi_id]
            values = {k: v for k, v in params.items() if k != id_param}
            if isinstance(values.get("art"), dict):
                merged = dict(row.get("art") or {})
                for name, value in values["art"].items():
                    if value is None:
                        merged.pop(name, None)
                    else:
                        merged[name] = value
                values["art"] = merged
            row.update(copy.deepcopy(values))
        return "OK"

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
            if params["content"] != "none":
                self.recursive[params["path"]] = bool(params.get("scanrecursive"))
                self.dirnames[params["path"]] = bool(params.get("usedirectorynames"))
            if params["content"] == "none":
                if (
                    params.get("clearmode") == "remove"
                    and not self.accept_without_apply
                ):
                    self.remove_content(params["path"])
                self.bindings.pop(params["path"], None)
                self.dirnames.pop(params["path"], None)
            else:
                self.bindings[params["path"]] = params["content"]
            return "OK"
        if method == "Settings.GetSettingValue":
            return {"value": self.settings.get(params["setting"])}
        if method == "Settings.SetSettingValue":
            self.settings[params["setting"]] = params["value"]
            return True
        if method == "Files.GetDirectory":
            self.listed.append(params["directory"])
            return {"files": [], "limits": {"total": 0}}
        if method == "VideoLibrary.Scan":
            self.scan(params["directory"])
            return "OK"
        if method == "AudioLibrary.Scan":
            return self.music_rpc(method, params)
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
            return self.music_rpc(method, params)
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
                    self.extras.pop(kodi_id, None)
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
    "Audio": ("songid", "songdetails", "songs"),
    "MusicAlbum": ("albumid", "albumdetails", "albums"),
    "MusicArtist": ("artistid", "artistdetails", "artists"),
}


def song_id_of(url):
    """The Jellyfin id a fake song row's URL carries."""
    return paths.parse_item(url)[1]
