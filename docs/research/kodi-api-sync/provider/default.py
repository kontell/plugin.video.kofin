"""Synthetic scanner provider; no network or SQL access."""

import json
import pathlib
import sys
import time
from urllib.parse import parse_qs, urlsplit

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

ADDON = "plugin.video.kofin.apiresearch"
BASE = "plugin://" + ADDON + "/"
PREFIX = "Kofin API research "
profile = pathlib.Path(xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile")))
profile.mkdir(parents=True, exist_ok=True)
config = (
    json.loads((profile / "config.json").read_text())
    if (profile / "config.json").exists()
    else {}
)
handle = int(sys.argv[1])
url = sys.argv[0] + (sys.argv[2] if len(sys.argv) > 2 else "")
route = urlsplit(url).path.strip("/")
query = parse_qs(urlsplit(url).query)
action = query.get("kodi_action", [""])[0]
with (profile / "trace.jsonl").open("a") as trace:
    trace.write(
        json.dumps({"route": route, "action": action, "time": time.time()}) + "\n"
    )


def video(kind, key, title):
    item = xbmcgui.ListItem(PREFIX + title, offscreen=True)
    tag = item.getVideoInfoTag()
    tag.setMediaType(kind)
    tag.setTitle(PREFIX + title)
    tag.setPlot("Synthetic original plot")
    tag.setUniqueID("kofin-api-research-" + key, "kofinresearch", True)
    tag.setGenres(["Research"])
    tag.setDateAdded("2020-01-02 03:04:05")
    tag.setPlaycount(3)
    tag.setLastPlayed("2020-02-03 04:05:06")
    tag.setResumePoint(12.0, 120.0)
    tag.setTags(["kofin-api-research"])
    tag.setCast([xbmc.Actor("Research Actor", "Synthetic role", 0, "")])
    tag.addVideoStream(xbmc.VideoStreamDetail(1920, 1080, 1.78, 120, "h264"))
    item.setProperty("IsPlayable", "true")
    item.setArt({"poster": "DefaultVideo.png", "kofin.downloaded": "DefaultVideo.png"})
    return item


def movie(i):
    item = video("movie", "movie-" + str(i), "Movie " + str(i))
    if config.get("movie_plot"):
        item.getVideoInfoTag().setPlot(config["movie_plot"])
    return item


def listing(entries, content):
    xbmcplugin.setContent(handle, content)
    xbmcplugin.addDirectoryItems(handle, entries, len(entries))
    xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)


if action == "check_exists":
    item = xbmcgui.ListItem(path=url, offscreen=True)
    xbmcplugin.setResolvedUrl(handle, not config.get("absent", False), item)
elif action == "refresh_info":
    if route in ("movies/query", "movies") and "id" in query:
        item = movie(1000 + int(query["id"][0]))
        listing([(url, item, False)], "movies")
    elif route.startswith("movies/"):
        item = movie(int(route.rsplit("/", 1)[1].split(".")[0]))
        listing([(url, item, False)], "movies")
    else:
        listing([], "videos")
elif route == "movies":
    if config.get("query_urls"):
        entries = [
            (BASE + "movies/?id=" + str(i), movie(1000 + i), False)
            for i in range(config.get("movies", 2))
        ]
    else:
        entries = [
            (BASE + "movies/" + str(i) + ".mkv", movie(i), False)
            for i in range(config.get("movies", 2))
        ]
    listing(entries, "movies")
elif route == "movies/query":
    listing([(BASE + "movies/query/?id=0", movie(1000), False)], "movies")
elif route == "shows":
    item = video("tvshow", "show", "Show")
    item.getVideoInfoTag().addSeason(1, "Research season one", "Research season plot")
    item.getVideoInfoTag().addSeason(2, "Research empty season", "Research empty plot")
    listing([(BASE + "shows/show/", item, True)], "tvshows")
elif route == "shows/show":
    item = video("episode", "episode", "Episode")
    item.getVideoInfoTag().setSeason(1)
    item.getVideoInfoTag().setEpisode(1)
    item.getVideoInfoTag().setSortSeason(1)
    item.getVideoInfoTag().setSortEpisode(2)
    listing([(BASE + "shows/show/s01e01.mkv", item, False)], "episodes")
elif route == "musicvideos":
    item = video("musicvideo", "musicvideo", "Music video")
    item.getVideoInfoTag().setArtists(["Research Video Artist"])
    item.getVideoInfoTag().setAlbum("Research Video Album")
    listing([(BASE + "musicvideos/song.mkv", item, False)], "musicvideos")
elif route in ("music", "music-modern"):
    entries = []
    modern = route == "music-modern"
    for i in range(config.get(route, 2)):
        title = PREFIX + ("Modern " if modern else "") + "Song " + str(i)
        item = xbmcgui.ListItem(title, offscreen=True)
        tag = item.getMusicInfoTag()
        tag.setMediaType("song")
        tag.setTitle(title)
        tag.setArtist("Kofin Research Artist")
        tag.setAlbum("Kofin Research " + route + " Album")
        tag.setAlbumArtist("Kofin Research Artist")
        tag.setTrack(i + 1)
        tag.setDuration(120)
        tag.setGenres(["Research"])
        tag.setPlayCount(3)
        tag.setLastPlayed("2020-02-03 04:05:06")
        if not modern:
            item.setInfo("music", {"title": title})
        elif config.get("modern_mark_loaded"):
            item.setInfo("music", {"size": 1024})
        item.setProperty("IsPlayable", "true")
        entries.append((BASE + route + "/track" + str(i) + ".mp3", item, False))
    listing(entries, "songs")
elif not route:
    listing(
        [
            (BASE + p + "/", xbmcgui.ListItem(p, offscreen=True), True)
            for p in ["movies", "shows", "musicvideos", "music", "music-modern"]
        ],
        "files",
    )
else:
    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem(offscreen=True))
