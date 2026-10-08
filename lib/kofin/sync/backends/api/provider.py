"""Scanner callbacks never contact Jellyfin or enumerate interactive menus."""

import re
from urllib.parse import urlsplit

import xbmcgui
import xbmcplugin

from . import metadata
from .store import MovieStore, playback_url


def key_from_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "plugin" or parsed.netloc != "plugin.video.kofin":
        return None
    match = re.fullmatch(r"/native/([0-9a-f]{32})/", parsed.path)
    return match.group(1) if match else None


def serve(request):
    key = key_from_url(request.base_url)
    action = request.params.get("kodi_action", "")
    if not key:
        # The advertised root lists only published scanner sources.
        from kofin.sync.private import Database

        with Database() as db:
            MovieStore("provider")._prepare(db.cursor)
            keys = [
                row[0]
                for row in db.cursor.execute(
                    "SELECT namespace FROM api_directory WHERE current>0"
                ).fetchall()
            ]
        from .store import directory

        entries = [
            (directory(k), xbmcgui.ListItem("Kofin movies", offscreen=True), True)
            for k in keys
        ]
        xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
        xbmcplugin.endOfDirectory(request.handle, cacheToDisc=False)
        return
    store = MovieStore(key)
    item_id = request.params.get("id")
    if action == "check_exists":
        # Unknown state is unavailable, never proof of deletion. Only an
        # explicit committed tombstone tells Kodi an imported item is gone.
        state = store.state(item_id) if item_id else None
        exists = not state or state.operation != "remove"
        xbmcplugin.setResolvedUrl(
            request.handle, exists, xbmcgui.ListItem(path=request.base_url)
        )
        return
    records, server, _ = store.snapshot()
    if action == "refresh_info":
        records = {item_id: records[item_id]} if item_id in records else {}
        if not records:
            xbmcplugin.endOfDirectory(
                request.handle, succeeded=False, cacheToDisc=False
            )
            return
    entries = [
        (
            playback_url(key, i),
            metadata.build(r["item"], server, key, r["library"]),
            False,
        )
        for i, r in sorted(records.items())
    ]
    xbmcplugin.setContent(request.handle, "movies")
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.endOfDirectory(request.handle, succeeded=True, cacheToDisc=False)
