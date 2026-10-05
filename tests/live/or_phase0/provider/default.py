"""Synthetic scanner fixture. Never opens Kodi databases or real media."""

import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

ADDON = "plugin.video.kofin.phase0"
BASE = "plugin://" + ADDON + "/"
PREFIX = "Kofin phase0 "


def video(kind, identity, title, config):
    item = xbmcgui.ListItem(PREFIX + title, offscreen=True)
    tag = item.getVideoInfoTag()
    tag.setMediaType(kind)
    tag.setTitle(PREFIX + title)
    tag.setPlot(config.get("plot", "Phase0 original plot"))
    tag.setUniqueID("kofin-phase0-" + identity, "kofinphase0", True)
    tag.setTags(["kofin-phase0"])
    tag.setPlaycount(2)
    tag.setResumePoint(12.0, 120.0)
    tag.setCast([xbmc.Actor("Kofin phase0 Actor", "Fixture", 0, "")])
    tag.addVideoStream(xbmc.VideoStreamDetail(1920, 1080, 1.78, 120, "h264"))
    item.setProperty("IsPlayable", "true")
    return item


def music(index, config):
    title = PREFIX + config.get("music_title", "Song") + " " + str(index)
    item = xbmcgui.ListItem(title, offscreen=True)
    tag = item.getMusicInfoTag()
    tag.setMediaType("song")
    tag.setTitle(title)
    tag.setArtist(PREFIX + "Artist")
    tag.setAlbum(PREFIX + "Album")
    tag.setAlbumArtist(PREFIX + "Artist")
    tag.setTrack(index + 1)
    tag.setDuration(120)
    tag.setPlayCount(3)
    tag.setLastPlayed("2020-02-03 04:05:06")
    if config.get("loaded", True):
        item.setInfo("music", {"size": config.get("music_size", 1024)})
    item.setProperty("IsPlayable", "true")
    return item


def main():
    profile = Path(xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile")))
    profile.mkdir(parents=True, exist_ok=True)
    config = json.loads((profile / "config.json").read_text())
    handle = int(sys.argv[1])
    url = sys.argv[0] + (sys.argv[2] if len(sys.argv) > 2 else "")
    route = urlsplit(url).path.strip("/")
    query = parse_qs(urlsplit(url).query)
    action = query.get("kodi_action", [""])[0]
    with (profile / "trace.jsonl").open("a") as trace:
        trace.write(
            json.dumps({"route": route, "action": action, "time": time.time()}) + "\n"
        )

    if action == "check_exists":
        xbmcplugin.setResolvedUrl(handle, True, xbmcgui.ListItem(path=url))
        return

    entries = []
    content = "files"
    if route == "movies":
        content = "movies"
        indexes = (
            [int(query["id"][0])]
            if action == "refresh_info"
            else range(config.get("movies", 2))
        )
        entries = [
            (
                BASE + "movies/?id=" + str(i),
                video("movie", "movie-" + str(i), "Movie " + str(i), config),
                False,
            )
            for i in indexes
        ]
    elif route == "shows":
        content = "tvshows"
        item = video("tvshow", "show", "Show", config)
        item.getVideoInfoTag().addSeason(1, "Phase0 season", "Phase0 season plot")
        entries = [(BASE + "shows/show/", item, True)]
    elif route == "shows/show":
        content = "episodes"
        item = video("episode", "episode", "Episode", config)
        tag = item.getVideoInfoTag()
        tag.setSeason(1)
        tag.setEpisode(1)
        tag.setSortSeason(1)
        tag.setSortEpisode(2)
        entries = [(BASE + "shows/show/?id=episode", item, False)]
    elif route == "musicvideos":
        content = "musicvideos"
        item = video("musicvideo", "musicvideo", "Music video", config)
        item.getVideoInfoTag().setArtists([PREFIX + "Video artist"])
        entries = [(BASE + "musicvideos/?id=clip", item, False)]
    elif route == "music":
        content = "songs"
        entries = [
            (BASE + "music/track" + str(i) + ".mp3", music(i, config), False)
            for i in range(config.get("music", 2))
        ]
        failure = config.get("music_failure", "")
        if failure == "partial":
            xbmcplugin.addDirectoryItems(handle, entries[:1], 1)
        if failure:
            xbmcplugin.endOfDirectory(handle, succeeded=False, cacheToDisc=False)
            return
    elif not route:
        entries = [
            (BASE + name + "/", xbmcgui.ListItem(name), True)
            for name in ("movies", "shows", "musicvideos", "music")
        ]
    else:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    xbmcplugin.setContent(handle, content)
    xbmcplugin.addDirectoryItems(handle, entries, len(entries))
    xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)


if __name__ == "__main__":
    main()
