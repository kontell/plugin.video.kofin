"""Run installed main's movie writer against private, disposable SQLite files.

This measures the persistence stage on fixed and real movie DTO snapshots,
not end-to-end sync or GUI responsiveness. Production Jellyfin access is GET
only. Native Kodi database paths are never passed to Database/sqlite3.
"""

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import xbmcaddon
import xbmcvfs

PROFILE = Path(
    xbmcvfs.translatePath("special://profile/addon_data/plugin.video.kofin.phase0")
)
INSTALLED = Path(xbmcvfs.translatePath("special://home/addons/plugin.video.kofin"))
sys.path.insert(0, str(INSTALLED / "lib"))

from kofin.core.api import Api  # noqa: E402
from kofin.core.settings import Credentials  # noqa: E402
from kofin.sync import db, downloader, kofindb  # noqa: E402
from kofin.sync.kodidb.kodi import Kodi  # noqa: E402
from kofin.sync.writers.movies import Movies  # noqa: E402


class ReadOnlyApi:
    """Expose only the GET operations used by movie writers; freeze replies."""

    def __init__(self, api=None):
        self.api = api
        self.server = api.server if api else "http://fixture.invalid"
        self.user_id = api.user_id if api else "phase0"
        self.cache = {}

    def _read(self, method, *args):
        key = json.dumps([method, args], sort_keys=True)
        if key not in self.cache:
            self.cache[key] = getattr(self.api, method)(*args) if self.api else []
        return copy.deepcopy(self.cache[key])

    def get(self, path, params=None):
        return self._read("get", path, params)

    def special_features(self, identity):
        return self._read("special_features", identity)

    def item(self, identity):
        return self._read("item", identity)


def initialize(directory):
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "video.db"
    with sqlite3.connect(str(path)) as conn:
        for name in ("myvideos149.sql", "myvideos149_seed.sql"):
            conn.executescript((PROFILE / name).read_text())
        conn.execute("INSERT INTO version(idVersion,iCompressCount) VALUES(149,0)")
    db.reset_overrides()
    for kind in db.KINDS:
        db.set_path_override(kind, str(directory / (kind + ".db")))
    Kodi.reset_people_cache()
    with db.Database("kofin") as mapping:
        kofindb.JellyfinDatabase(mapping.cursor).add_view("phase0", "Phase0", "movies")


def counts(directory):
    with sqlite3.connect(str(directory / "video.db")) as conn:
        movies = conn.execute("SELECT COUNT(*) FROM movie").fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    with sqlite3.connect(str(directory / "kofin.db")) as conn:
        mappings = conn.execute(
            "SELECT COUNT(*) FROM jellyfin WHERE media_type='movie'"
        ).fetchone()[0]
    return {"movies": movies, "files": files, "mappings": mappings}


def apply(api, rows, operation="movie"):
    start = time.perf_counter()
    refused = 0
    with db.Database("kofin") as mapping, db.Database("video") as video:
        writer = Movies(api, mapping, video, library={"Id": "phase0", "Name": "Phase0"})
        for index, row in enumerate(rows, 1):
            getattr(writer, operation)(copy.deepcopy(row))
            if index % 50 == 0:
                video.conn.commit()
                mapping.conn.commit()
        refused = len(writer.refused)
    return {
        "seconds": round(time.perf_counter() - start, 6),
        "items": len(rows),
        "refused": refused,
    }


def benchmark(api, rows, directory):
    initialize(directory)
    result = {"initial": apply(api, rows), "after_initial": counts(directory)}
    result["unchanged"] = apply(api, rows)
    result["after_unchanged"] = counts(directory)
    delta = copy.deepcopy(rows[:10])
    for row in delta:
        row["Etag"] = str(row.get("Etag", "")) + "-phase0-delta"
        row["Overview"] = "Kofin phase0 changed metadata"
    result["metadata_delta"] = apply(api, delta)
    with sqlite3.connect(str(directory / "video.db")) as conn:
        result["metadata_verified"] = conn.execute(
            "SELECT COUNT(*) FROM movie WHERE c01=?",
            ("Kofin phase0 changed metadata",),
        ).fetchone()[0] == len(delta)
    for row in delta:
        row["UserData"].update(
            Played=True, PlayCount=7, PlaybackPositionTicks=120000000
        )
    result["userdata_delta"] = apply(api, delta, "userdata")
    with sqlite3.connect(str(directory / "video.db")) as conn:
        result["userdata_verified"] = conn.execute(
            "SELECT COUNT(*) FROM movie JOIN files USING(idFile) WHERE c01=? AND playCount=7",
            ("Kofin phase0 changed metadata",),
        ).fetchone()[0] == len(delta)
    result["deletion"] = apply(api, [row["Id"] for row in delta], "remove")
    result["after_delete"] = counts(directory)
    result["verified"] = (
        result["after_initial"]["movies"] == len(rows)
        and result["after_initial"] == result["after_unchanged"]
        and result["after_delete"]["movies"] == len(rows) - len(delta)
        and result["after_delete"]["mappings"] == len(rows) - len(delta)
        and result["metadata_verified"]
        and result["userdata_verified"]
        and all(
            result[op]["refused"] == 0
            for op in (
                "initial",
                "unchanged",
                "metadata_delta",
                "userdata_delta",
                "deletion",
            )
        )
    )
    return result


def main():
    run = PROFILE / "sql-baseline" / str(time.time_ns())
    run.mkdir(parents=True)

    def restrict_sqlite(event, args):
        if event == "sqlite3.connect" and not Path(args[0]).resolve().is_relative_to(
            run.resolve()
        ):
            raise RuntimeError(
                "benchmark attempted to open SQLite outside its disposable directory"
            )

    sys.addaudithook(restrict_sqlite)
    result = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "installed_version": xbmcaddon.Addon("plugin.video.kofin").getAddonInfo(
            "version"
        ),
        "scope": "movie persistence stage; private MyVideos149 + kofin.db; no pipeline hooks, GUI work or artwork downloads",
        "commit_interval": 50,
        "samples": {},
        "source_hashes": {
            str(p.relative_to(INSTALLED)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((INSTALLED / "lib/kofin/sync").rglob("*.py"))
        },
    }
    output = PROFILE / "sql-baseline.json"
    try:
        fixed = json.loads((PROFILE / "fixed-movies.json").read_text())
        real_api = Api.for_plugin(Credentials.load())
        rows = []
        start = time.perf_counter()
        total = None
        while total is None or len(rows) < total:
            page = real_api.items(
                {
                    "IncludeItemTypes": "Movie",
                    "Recursive": True,
                    "Fields": downloader.info(),
                    "StartIndex": len(rows),
                    "Limit": 100,
                    "SortBy": "SortName",
                    "SortOrder": "Ascending",
                    "EnableTotalRecordCount": True,
                }
            )
            total = page["TotalRecordCount"]
            if not page.get("Items") or total > 10000:
                raise RuntimeError("incomplete or unexpectedly large source catalogue")
            rows.extend(page["Items"])
        result["fetch"] = {
            "movies": len(rows),
            "seconds": round(time.perf_counter() - start, 6),
            "sha256": hashlib.sha256(
                json.dumps(rows, sort_keys=True).encode()
            ).hexdigest(),
        }
        # Raw production metadata stays inside the temporary research profile.
        (run / "private-movies.json").write_text(json.dumps(rows))
        proxy = ReadOnlyApi(real_api)
        for row in rows:
            if row.get("SpecialFeatureCount"):
                proxy.special_features(row["Id"])
        result["child_replies_prefetched"] = len(proxy.cache)
        for name, api, source in (
            ("fixed100", ReadOnlyApi(), fixed),
            ("real_movies", proxy, rows),
        ):
            result["samples"][name] = []
            for sample in range(3):
                result["samples"][name].append(
                    benchmark(api, source, run / (name + "-" + str(sample)))
                )
                output.write_text(json.dumps(result, indent=2) + "\n")
    except Exception as error:
        result["error_type"] = type(error).__name__
        # Message goes to a private diagnostic file, never the evidence bundle.
        (PROFILE / "sql-baseline-error.txt").write_text(str(error))
    finally:
        db.reset_overrides()
        result["complete"] = True
        output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
