"""Run real FullSync and delta workers in Kodi against owned fixture databases.

Invoked by probe.py with SOURCE and WORK. No native Kodi database is opened.
The server DTOs and child replies are synthetic; UI refreshes are recorded.
"""

import copy
import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import threading
import time

from kofin.core import settings, state
from kofin.sync import db, kofindb
from kofin.sync.full_sync import FullSync
from kofin.sync.workers import UpdateWorker, UserDataWorker, RemovedWorker
from fixture_dtos import (
    MOVIE,
    SERIES,
    SEASON_1,
    EPISODE,
    MUSICVIDEO,
    ARTIST,
    ALBUM,
    SONG,
    LIBRARY,
    TV_LIBRARY,
    MV_LIBRARY,
    MUSIC_LIBRARY,
)
from fixture_api import FakeApi


class Api(FakeApi):
    def __init__(self):
        super().__init__()
        self.movies = []
        for i in range(100):
            row = copy.deepcopy(MOVIE)
            row.update(
                Id="%032x" % (i + 1000),
                Name="Phase1 movie %03d" % i,
                SortName="Phase1 movie %03d" % i,
                ProviderIds={},
                Path="/fixture/movie%03d.mkv" % i,
            )
            self.movies.append(row)
        self.rows = self.movies + [
            copy.deepcopy(x)
            for x in (SERIES, SEASON_1, EPISODE, MUSICVIDEO, ARTIST, ALBUM, SONG)
        ]
        self.items_by_id = {x["Id"]: x for x in self.rows}
        self.seasons_by_series = {SERIES["Id"]: [copy.deepcopy(SEASON_1)]}
        self.groups = {
            "Movie": self.movies,
            "Series": [SERIES],
            "Season": [SEASON_1],
            "Episode": [EPISODE],
            "MusicVideo": [MUSICVIDEO],
            "MusicArtist": [ARTIST],
            "MusicAlbum": [ALBUM],
            "Audio": [SONG],
        }

    def ancestors(self, item_id):
        kind = self.items_by_id[item_id]["Type"]
        library = (
            TV_LIBRARY
            if kind in ("Series", "Season", "Episode")
            else (
                MUSIC_LIBRARY
                if kind in ("MusicArtist", "MusicAlbum", "Audio")
                else MV_LIBRARY if kind == "MusicVideo" else LIBRARY
            )
        )
        return [library]

    def get(self, path, params=None):
        params = params or {}
        if path == "/Artists" or (path == "/Items" and params.get("IncludeItemTypes")):
            types = params.get("IncludeItemTypes", "MusicArtist").split(",")
            rows = [r for kind in types for r in self.groups.get(kind, [])]
            start, limit = int(params.get("StartIndex", 0)), int(
                params.get("Limit", 50)
            )
            return {
                "Items": copy.deepcopy(rows[start : start + limit]),
                "TotalRecordCount": len(rows),
            }
        return super().get(path, params)


class Host:
    def __init__(self):
        self.database_lock = threading.Lock()
        self.music_database_lock = threading.Lock()


class Dialog:
    def update(self, *args, **kwargs):
        pass


def fingerprint(work):
    result = {}
    for kind in ("video", "music", "kofin"):
        conn = sqlite3.connect(str(work / (kind + ".db")))
        tables = [
            x[0]
            for x in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        contents = {}
        for table in tables:
            if table in ("backend_state", "versiontagscan"):
                continue
            rows = conn.execute('SELECT * FROM "' + table + '"').fetchall()
            # MyMusic stamps its scanner/trigger wall clocks independently of
            # server metadata. Retain nullness, normalize only those clocks.
            columns = [r[1] for r in conn.execute('PRAGMA table_info("' + table + '")')]
            if kind == "music":
                clocks = {"lastScraped", "dateNew", "dateModified"}
                rows = [
                    tuple(
                        (
                            "<runtime-clock>"
                            if columns[i] in clocks and value is not None
                            else value
                        )
                        for i, value in enumerate(row)
                    )
                    for row in rows
                ]
            if rows:
                contents[table] = sorted(rows, key=repr)
        conn.close()
        raw = json.dumps(contents, sort_keys=True)
        (work / (kind + "-rows.json")).write_text(raw)
        result[kind] = hashlib.sha256(raw.encode()).hexdigest()
    return result


def require(condition, message="phase1 invariant failed"):
    # Kodi can compile scripts with assertions disabled. Checks and their
    # side effects must execute under Python optimization as well.
    if not condition:
        raise RuntimeError(message)


def main(source, work, fixtures):
    work.mkdir(parents=True, exist_ok=False)

    def guard(event, args):
        if event == "sqlite3.connect":
            require(
                (Path(args[0]).resolve().is_relative_to(work.resolve())),
                ("non-fixture SQLite access"),
            )

    import sys

    sys.addaudithook(guard)
    for kind, stem, version in (
        ("video", "myvideos149", 149),
        ("music", "mymusic84", 84),
    ):
        with sqlite3.connect(str(work / (kind + ".db"))) as conn:
            conn.executescript((fixtures / (stem + ".sql")).read_text())
            conn.executescript((fixtures / (stem + "_seed.sql")).read_text())
            conn.execute(
                "INSERT INTO version(idVersion,iCompressCount) VALUES(?,0)", (version,)
            )
    db.reset_overrides()
    for kind in db.KINDS:
        db.set_path_override(kind, str(work / (kind + ".db")))
    db.ADDON_DATA = str(work) + "/"
    try:
        from kofin.sync import private

        private.ADDON_DATA = str(work) + "/"
    except ImportError:
        pass
    settings.addon_data_path = lambda: str(work)
    state.set_online(True)
    libraries = (LIBRARY, TV_LIBRARY, MV_LIBRARY, MUSIC_LIBRARY)
    initial = {
        "Libraries": [],
        "Whitelist": [v["Id"] for v in libraries],
        "SortedViews": [],
        "RestorePoints": {},
    }
    db.save_sync(initial)
    with db.Database("kofin") as mapping:
        store = kofindb.JellyfinDatabase(mapping.cursor)
        for view, media in zip(
            libraries, ("movies", "tvshows", "musicvideos", "music")
        ):
            store.add_view(view["Id"], view["Name"], media)
    api, host = Api(), Host()
    sync = FullSync(
        host, api, loader=lambda: copy.deepcopy(initial), saver=lambda value: None
    )
    sync.sync = copy.deepcopy(initial)
    timings, hashes, row_counts = {}, {}, {}

    def counts():
        result = {}
        for kind, tables in (
            ("video", ("movie", "tvshow", "episode", "musicvideo")),
            ("music", ("song", "album", "artist")),
        ):
            with sqlite3.connect(str(work / (kind + ".db"))) as conn:
                result.update(
                    {
                        table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[
                            0
                        ]
                        for table in tables
                    }
                )
        return result

    for name in ("initial", "unchanged"):
        began = time.perf_counter()
        for method, library in zip(
            ("movies", "tvshows", "musicvideos", "music"), libraries
        ):
            getattr(FullSync, method).__wrapped__(sync, library, dialog=Dialog())
        timings[name] = round(time.perf_counter() - began, 6)
        hashes[name] = fingerprint(work)
        row_counts[name] = counts()
    failures = []
    for stage, cls in (
        ("metadata", UpdateWorker),
        ("userdata", UserDataWorker),
        ("remove", RemovedWorker),
    ):
        began = time.perf_counter()
        for kind, rows in (("video", [api.movies[0], EPISODE]), ("music", [SONG])):
            work_queue = queue.Queue()
            for original in rows:
                item = copy.deepcopy(original)
                item["Etag"] += "-phase1"
                item["Overview"] = "Phase1 revised metadata"
                if stage == "userdata":
                    item["UserData"].update(
                        Played=True, PlayCount=7, PlaybackPositionTicks=120000000
                    )
                work_queue.put(item)
            args = (
                (work_queue, queue.Queue(), threading.Lock(), kind, api)
                if cls is UpdateWorker
                else (work_queue, threading.Lock(), kind, api)
            )
            worker = cls(*args, unapplied=lambda *args: failures.append(args))
            worker.run()
            require((worker.is_done), ("phase1 invariant failed"))
        timings[stage] = round(time.perf_counter() - began, 6)
        hashes[stage] = fingerprint(work)
        row_counts[stage] = counts()
    result = {
        "timings": timings,
        "row_counts": row_counts,
        "hashes": hashes,
        "unapplied": len(failures),
        "restore_points_cleared": not sync.sync["RestorePoints"],
    }
    require(
        row_counts["initial"]
        == {
            "movie": 100,
            "tvshow": 1,
            "episode": 1,
            "musicvideo": 1,
            "song": 1,
            "album": 1,
            "artist": 2,
        },
        "fixture did not exercise all media",
    )
    require((not failures), (failures))
    return result
