"""Scanner callbacks never contact Jellyfin or enumerate interactive menus.

Every listing comes from the pinned committed generation, so a scan that
spans minutes sees one complete membership throughout, and nothing here can
turn a server outage into an empty directory.
"""

from typing import Any, Dict, List

import xbmcgui
import xbmcplugin

from . import metadata, paths
from .store import Store

key_from_url = paths.key_from_url


def _roots():
    """Every bound scanner root, for Kodi's own walk of the plugin."""
    from kofin.sync.private import Database

    with Database() as db:
        Store("provider")._prepare(db.cursor)
        return [
            row[0]
            for row in db.cursor.execute(
                "SELECT path FROM api_binding ORDER BY path"
            ).fetchall()
        ]


def _listing(request, entries, content):
    if content:
        xbmcplugin.setContent(request.handle, content)
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.endOfDirectory(request.handle, succeeded=True, cacheToDisc=False)


def _folders(request, directories, label):
    _listing(
        request,
        [(d, xbmcgui.ListItem(label, offscreen=True), True) for d in directories],
        "",
    )


def exists(store, location, item_id):
    """Whether Kodi may keep a row: unknown state is unavailable, never proof
    of deletion. Only an explicit committed tombstone says an item is gone."""
    if item_id:
        state = store.state(item_id)
        return not state or state.operation != "remove"
    if location.series:
        state = store.state(location.series)
        return not state or state.operation != "remove"
    if location.library:
        prefix = paths.library_root(store.namespace, location.library)
        if any(path.startswith(prefix) for path in store.bindings()):
            return True
        return bool(store.records(library=location.library, pinned=False))
    return True


def serve(request):
    location = paths.parse(request.base_url)
    action = request.params.get("kodi_action", "")
    if location is None:
        if request.base_url.rstrip("/").endswith("/native"):
            _folders(request, _roots(), "Kofin")
            return
        xbmcplugin.endOfDirectory(request.handle, succeeded=False, cacheToDisc=False)
        return
    store = Store(location.key)
    item_id = request.params.get("id")
    if action == "check_exists":
        xbmcplugin.setResolvedUrl(
            request.handle,
            exists(store, location, item_id),
            xbmcgui.ListItem(path=request.base_url),
        )
        return
    key = location.key
    if location.library is None or location.content is None:
        prefix = (
            paths.library_root(key, location.library)
            if location.library
            else paths.root(key)
        )
        _folders(
            request,
            sorted(p for p in store.bindings() if p.startswith(prefix)),
            "Kofin",
        )
        return
    server = store.server()
    separator = metadata.item_separator()
    library = location.library
    if location.content == "tvshows":
        if location.series is None:
            shows = store.records(kind="Series", library=library)
            seasons: Dict[str, List[Dict[str, Any]]] = {}
            for record in store.records(kind="Season", library=library).values():
                seasons.setdefault(record.parent_id, []).append(record.item)
            episodes: Dict[str, List[Dict[str, Any]]] = {}
            for record in store.records(kind="Episode", library=library).values():
                episodes.setdefault(record.parent_id, []).append(record.item)
            entries = [
                (
                    paths.show_dir(key, library, series_id),
                    metadata.listitem(
                        record.item,
                        server,
                        key,
                        library,
                        separator,
                        seasons.get(series_id, []),
                        episodes.get(series_id, []),
                    ),
                    True,
                )
                for series_id, record in sorted(shows.items())
            ]
            _listing(request, entries, "tvshows")
            return
        series_id = location.series
        if action == "refresh_info" and not item_id:
            show = store.records(kind="Series", item_ids=[series_id]).get(series_id)
            if show is None:
                xbmcplugin.endOfDirectory(
                    request.handle, succeeded=False, cacheToDisc=False
                )
                return
            entry = (
                paths.show_dir(key, library, series_id),
                metadata.listitem(
                    show.item,
                    server,
                    key,
                    library,
                    separator,
                    [
                        r.item
                        for r in store.records(
                            kind="Season", parent_id=series_id
                        ).values()
                    ],
                    [
                        r.item
                        for r in store.records(
                            kind="Episode", parent_id=series_id
                        ).values()
                    ],
                ),
                True,
            )
            _listing(request, [entry], "tvshows")
            return
        records = store.records(
            kind="Episode",
            parent_id=series_id,
            item_ids=[item_id] if action == "refresh_info" else None,
        )
        kind, content = "Episode", "episodes"
    else:
        kind = "Movie" if location.content == "movies" else "MusicVideo"
        content = location.content
        records = store.records(
            kind=kind,
            library=library,
            item_ids=[item_id] if action == "refresh_info" else None,
        )
    if action == "refresh_info" and not records:
        xbmcplugin.endOfDirectory(request.handle, succeeded=False, cacheToDisc=False)
        return
    collections = (
        metadata.collections_of(r.item for r in store.records(kind="BoxSet").values())
        if kind == "Movie"
        else {}
    )
    entries = []
    for record_id, record in sorted(records.items()):
        if kind == "Episode" and metadata.episode_numbers(record.item) is None:
            # Kodi cannot file an unnumbered special; it stays dynamic-only.
            continue
        entries.append(
            (
                paths.playback_url(key, kind, library, record_id, record.parent_id),
                metadata.listitem(
                    record.item,
                    server,
                    key,
                    library,
                    separator,
                    set_name=(
                        collections.get(record_id, "") if kind == "Movie" else None
                    ),
                ),
                False,
            )
        )
    _listing(request, entries, content)
