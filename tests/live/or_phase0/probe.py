"""Run inside Kodi. All mutations are confined to plugin.video.kofin.phase0.

RunScript(special://profile/addon_data/plugin.video.kofin.phase0/probe.py,MODE)
Modes: capture, lifecycle, prepare-forced, read-forced, cleanup.
Host automation retrieves JSON results; no credentials or real titles are saved.
"""

import json
from pathlib import Path
import platform
import sys
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = "plugin.video.kofin.phase0"
BASE = "plugin://" + ADDON + "/"
PROFILE = Path(xbmcvfs.translatePath("special://profile/addon_data/" + ADDON))


def rpc(method, params=None):
    response = json.loads(
        xbmc.executeJSONRPC(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            )
        )
    )
    if "error" in response:
        raise RuntimeError(method + ": " + json.dumps(response["error"]))
    return response["result"]


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.events = []

    def onScanStarted(self, library):
        self.events.append(("started", library, time.monotonic()))

    def onScanFinished(self, library):
        self.events.append(("finished", library, time.monotonic()))


def configure(**values):
    path = PROFILE / "config.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    config.update(values)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(config))
    temp.replace(path)


def scan(monitor, library, route):
    if not route or route.startswith("/") or ".." in route:
        raise ValueError("fixture route required")
    if xbmc.getCondVisibility("Library.IsScanningVideo") or xbmc.getCondVisibility(
        "Library.IsScanningMusic"
    ):
        raise RuntimeError("another scan is active")
    offset = len(monitor.events)
    start = time.monotonic()
    result = rpc(
        library + "Library.Scan", {"directory": BASE + route, "showdialogs": False}
    )
    deadline = start + 45
    while time.monotonic() < deadline:
        events = monitor.events[offset:]
        expected = "music" if library == "Audio" else "video"
        if any(
            event[0] == "finished" and event[1].lower() == expected for event in events
        ):
            return {
                "accepted": result,
                "seconds": round(time.monotonic() - start, 4),
                "events": [[event[0], event[1]] for event in events],
            }
        if monitor.waitForAbort(0.1):
            raise RuntimeError("Kodi is shutting down")
    raise RuntimeError("scan completion timed out")


def media(kind):
    methods = {
        "movies": (
            "VideoLibrary.GetMovies",
            [
                "file",
                "plot",
                "uniqueid",
                "playcount",
                "resume",
                "cast",
                "streamdetails",
            ],
        ),
        "tvshows": ("VideoLibrary.GetTVShows", ["file", "uniqueid"]),
        "episodes": (
            "VideoLibrary.GetEpisodes",
            ["file", "showtitle", "season", "episode"],
        ),
        "musicvideos": ("VideoLibrary.GetMusicVideos", ["file", "uniqueid"]),
        "songs": (
            "AudioLibrary.GetSongs",
            ["file", "title", "album", "artist", "playcount", "lastplayed"],
        ),
    }
    method, properties = methods[kind]
    rows = rpc(
        method,
        {
            "properties": properties,
            "filter": {
                "field": "title",
                "operator": "startswith",
                "value": "Kofin phase0 ",
            },
        },
    ).get(kind, [])
    return [row for row in rows if row.get("file", "").startswith(BASE)]


def bind(route, content):
    params = {
        "path": BASE + route,
        "content": content,
        "scraperid": "metadata.local",
        "scanrecursive": False,
        "refresh": False,
    }
    if route == "shows/show/":
        params["containssingleitem"] = True
    return rpc("VideoLibrary.SetSourceContent", params)


def capture():
    schema = rpc("JSONRPC.Introspect", {"getdescriptions": True, "getmetadata": True})
    methods = {
        name: definition
        for name, definition in schema["methods"].items()
        if name.startswith(("VideoLibrary.", "AudioLibrary.", "Textures."))
        or name in ("Files.SetFileDetails", "Files.GetDirectory")
    }
    tag_video = xbmcgui.ListItem(offscreen=True).getVideoInfoTag()
    tag_music = xbmcgui.ListItem(offscreen=True).getMusicInfoTag()
    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "application": rpc(
            "Application.GetProperties", {"properties": ["version", "name"]}
        ),
        "jsonrpc": rpc("JSONRPC.Version"),
        "python": platform.python_version(),
        "system_addons": {
            name: xbmcaddon.Addon(name).getAddonInfo("version")
            for name in ("xbmc.addon", "xbmc.python")
        },
        "installed_kofin": xbmcaddon.Addon("plugin.video.kofin").getAddonInfo(
            "version"
        ),
        "python_video_methods": {
            name: hasattr(tag_video, name)
            for name in (
                "setUniqueID",
                "setCast",
                "addVideoStream",
                "addSeason",
                "setSortSeason",
                "setSortEpisode",
                "setTags",
                "setVideoAssetTitle",
            )
        },
        "python_music_methods": {
            name: hasattr(tag_music, name)
            for name in (
                "setTitle",
                "setArtist",
                "setAlbum",
                "setMusicBrainzTrackID",
                "setPlayCount",
                "setLastPlayed",
                "setLoaded",
            )
        },
        "methods": methods,
        "types": schema["types"],
        "active_players": rpc("Player.GetActivePlayers"),
        "totals": {
            kind: rpc(method, {"limits": {"start": 0, "end": 1}})["limits"]["total"]
            for kind, method in (
                ("movies", "VideoLibrary.GetMovies"),
                ("shows", "VideoLibrary.GetTVShows"),
                ("episodes", "VideoLibrary.GetEpisodes"),
                ("musicvideos", "VideoLibrary.GetMusicVideos"),
                ("songs", "AudioLibrary.GetSongs"),
                ("albums", "AudioLibrary.GetAlbums"),
                ("artists", "AudioLibrary.GetArtists"),
            )
        },
    }


def lifecycle(monitor, result):
    configure(
        movies=2,
        music=2,
        loaded=True,
        music_size=1024,
        music_title="Song",
        music_failure="",
        plot="Phase0 original plot",
    )
    for route, content in (
        ("movies/", "movies"),
        ("shows/", "tvshows"),
        ("shows/show/", "tvshows"),
        ("musicvideos/", "musicvideos"),
    ):
        result["bind_" + route] = bind(route, content)
    for route in ("movies/", "shows/", "musicvideos/"):
        result["scan_" + route] = scan(monitor, "Video", route)
    result["video_initial"] = {
        kind: media(kind) for kind in ("movies", "tvshows", "episodes", "musicvideos")
    }
    rows = media("movies")
    if len(rows) != 2:
        raise RuntimeError("movie insertion did not produce exactly two fixture rows")
    movie = rows[0]
    rpc(
        "VideoLibrary.SetMovieDetails",
        {
            "movieid": movie["movieid"],
            "plot": "Phase0 patched plot",
            "playcount": 4,
            "resume": {"position": 25, "total": 120},
        },
    )
    result["movie_patch"] = media("movies")
    configure(plot="Phase0 refreshed plot")
    rpc("VideoLibrary.RefreshMovie", {"movieid": movie["movieid"], "ignorenfo": False})
    for _ in range(200):
        rows = media("movies")
        if any(
            row["file"] == movie["file"] and row.get("plot") == "Phase0 refreshed plot"
            for row in rows
        ):
            break
        monitor.waitForAbort(0.1)
    result["movie_refresh"] = rows
    result["music_import"] = scan(monitor, "Audio", "music/")
    result["music_initial"] = media("songs")
    configure(music_title="Metadata changed")
    result["metadata_only_scan"] = scan(monitor, "Audio", "music/")
    result["metadata_only_result"] = media("songs")
    configure(music_size=2048)
    result["size_changed_scan"] = scan(monitor, "Audio", "music/")
    result["size_changed_result"] = media("songs")
    for failure in ("before", "partial"):
        configure(
            music_failure="", music=2, music_size=4096 if failure == "before" else 5120
        )
        result["failed_" + failure + "_baseline_scan"] = scan(
            monitor, "Audio", "music/"
        )
        result["failed_" + failure + "_baseline"] = media("songs")
        configure(music_failure=failure)
        result["failed_" + failure + "_scan"] = scan(monitor, "Audio", "music/")
        result["failed_" + failure + "_result"] = media("songs")
    configure(music_failure="", music=1, music_size=3072)
    result["remove_one_scan"] = scan(monitor, "Audio", "music/")
    result["remove_one_result"] = media("songs")
    configure(music=0)
    result["remove_all_scan"] = scan(monitor, "Audio", "music/")
    result["remove_all_result"] = media("songs")
    return result


def cleanup(monitor):
    configure(music=0, music_failure="")
    scan(monitor, "Audio", "music/")
    for kind, singular in (
        ("movies", "Movie"),
        ("tvshows", "TVShow"),
        ("musicvideos", "MusicVideo"),
    ):
        for row in media(kind):
            rpc(
                "VideoLibrary.Remove" + singular,
                {singular.lower() + "id": row[singular.lower() + "id"]},
            )
    for route in ("movies/", "shows/show/", "shows/", "musicvideos/"):
        rpc(
            "VideoLibrary.SetSourceContent",
            {"path": BASE + route, "content": "none", "clearmode": "remove"},
        )
    remaining = {
        kind: media(kind)
        for kind in ("movies", "tvshows", "episodes", "musicvideos", "songs")
    }
    return {"remaining": remaining, "clean": not any(remaining.values())}


def main():
    PROFILE.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[1]
    monitor = Monitor()
    result = {
        "mode": mode,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        if mode == "capture":
            result["result"] = capture()
        elif mode == "lifecycle":
            result["result"] = {}
            lifecycle(monitor, result["result"])
        elif mode == "prepare-forced":
            configure(
                music=2,
                loaded=True,
                music_size=4096,
                music_failure="",
                music_title="Forced scan",
            )
            result["scan"] = scan(monitor, "Audio", "music/")
            result["songs"] = media("songs")
        elif mode == "read-forced":
            result["songs"] = media("songs")
        elif mode == "set-forced-metadata":
            configure(music_title="Forced metadata changed")
            result["songs_before_rescan"] = media("songs")
        elif mode == "cleanup":
            result["result"] = cleanup(monitor)
        else:
            raise ValueError("unknown mode")
    except Exception as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)}
    (PROFILE / (mode + ".json")).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
