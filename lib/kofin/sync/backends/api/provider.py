"""Scanner callbacks never contact Jellyfin or enumerate interactive menus.

Every listing comes from the pinned committed generation, so a scan that
spans minutes sees one complete membership throughout, and nothing here can
turn a server outage into an empty directory.

A music listing cannot fail halfway. Kodi scans whatever rows reached it
before a listing failed, however it failed, and the music scanner replaces a
directory's songs with what it was given -- so a song directory is built in
full before anything is handed over, goes out in one call, never ends with
``succeeded=False``, and when the store itself cannot be read the rows Kodi
already holds are listed back to it rather than nothing.
"""

import time
from typing import Any, Dict, List

import xbmcgui
import xbmcplugin

import xbmc

from kofin.core import kodirpc, state
from kofin.sync import private
from kofin.core.log import Logger
from . import metadata, paths
from .store import PayloadWindow, Store

LOG = Logger(__name__)

# The longest a hold listing stays open on its own: the pass releases it,
# and a pass that died takes at most this long to let the scanner go.
HOLD_LIMIT = 600.0

key_from_url = paths.key_from_url


def _roots():
    """Every scanner root, for Kodi's own walk of the plugin: the bound video
    roots, and the music root of every library with a song."""
    from kofin.sync.private import Database

    with Database() as db:
        Store("provider")._prepare(db.cursor)
        roots = [
            row[0]
            for row in db.cursor.execute(
                "SELECT path FROM api_binding ORDER BY path"
            ).fetchall()
        ]
        for namespace, library in db.cursor.execute(
            "SELECT DISTINCT namespace, library FROM api_entry"
            " WHERE kind='Audio' AND removed IS NULL"
        ).fetchall():
            roots.append(paths.library_dir(namespace, library, "music"))
    return sorted(set(roots))


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
    if location.folder or location.movie:
        return True
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
    # One database connection for the life of this interpreter: a scanner
    # that reuses it between listings then pays the open once.
    private.pool_connections(True)
    location = paths.parse(request.base_url)
    action = request.params.get("kodi_action", "")
    if location is None:
        if request.base_url.rstrip("/").endswith("/native"):
            _folders(request, _roots(), "Kofin")
            return
        xbmcplugin.endOfDirectory(request.handle, succeeded=False, cacheToDisc=False)
        return
    if location.hold:
        if action == "check_exists":
            xbmcplugin.setResolvedUrl(
                request.handle, True, xbmcgui.ListItem(path=request.base_url)
            )
            return
        _hold(request, location.hold)
        return
    store = Store(location.key)
    item_id = request.params.get("id") or location.song
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
        roots = {p for p in store.bindings() if p.startswith(prefix)}
        roots.update(
            paths.library_dir(key, library, "music")
            for library in store.libraries("Audio")
            if paths.library_dir(key, library, "music").startswith(prefix)
        )
        _folders(request, sorted(roots), "Kofin")
        return
    library = location.library
    if location.content == "music":
        if location.folder is None:
            _music_root(request, store, key, library)
        else:
            _music_folder(request, store, key, library, location.folder)
        return
    server = store.server()
    separator = metadata.item_separator()
    if location.content == "tvshows":
        if location.series is None:
            shows = store.records(kind="Series", library=library)
            seasons: Dict[str, List[Dict[str, Any]]] = {}
            for record in store.records(kind="Season", library=library).values():
                seasons.setdefault(record.parent_id, []).append(record.item)
            # Only the numbering reaches the show hash; the payloads are read
            # a chunk at a time and let go. Held together, one library's
            # episodes were over 100 MB in the listing's interpreter.
            episodes: Dict[str, List[Any]] = {}
            window = PayloadWindow(store)
            lazy = store.records(kind="Episode", library=library, payloads=False)
            for record in window.walk(lazy.values()):
                row = metadata.episode_row(record.item)
                if row is not None:
                    episodes.setdefault(record.parent_id, []).append(row)
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
            payloads=False,
        )
        kind, content = "Episode", "episodes"
    else:
        # A movie's URL sits in a folder of its own (paths.movie_dir), so the
        # root lists every movie as a file -- never as folders: Kodi turns a
        # plugin folder in a movies listing into a phantom VIDEO_TS.IFO,
        # because CPluginFile::Exists is unconditionally true
        # (FileItemList.cpp ConvertDiscFoldersToFiles) -- and a folder, bound
        # on demand, lists its one movie for a scan by name.
        kind = "Movie" if location.content == "movies" else "MusicVideo"
        content = location.content
        wanted = [item_id] if action == "refresh_info" else None
        if kind == "Movie" and location.movie:
            wanted = [location.movie]
        records = store.records(
            kind=kind,
            library=library,
            item_ids=wanted,
            payloads=False,
        )
    if action == "refresh_info" and not records:
        xbmcplugin.endOfDirectory(request.handle, succeeded=False, cacheToDisc=False)
        return
    collections = (
        metadata.collections_of(r.item for r in store.records(kind="BoxSet").values())
        if kind == "Movie"
        else {}
    )
    # Payloads are read a chunk at a time and let go once their ListItem is
    # built: one library's movies held together were about 100 MB in the
    # listing's interpreter, which a 1 GB device did not have (its first
    # movies listing under the scanner left it unable to answer ssh).
    entries = []
    window = PayloadWindow(store)
    for record in window.walk(records[i] for i in sorted(records)):
        record_id = record.item_id
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


# -- music ---------------------------------------------------------------------


def _music_root(request, store, key, library):
    """The library's album directories: every live one, and every one that
    still has songs to shed. A tombstone leaves Kodi only through the
    complete listing of its directory, so a directory whose songs are all
    gone stays listed, empty, until the scan has confirmed it."""
    folders = set(store.folders("Audio", library))
    folders.update(
        placed.parent_id
        for placed in store.tombstones().values()
        if placed.kind == "Audio" and placed.library == library and placed.parent_id
    )
    albums = store.records(kind="MusicAlbum", library=library) if folders else {}
    entries = []
    for folder in sorted(folders):
        album = albums.get(folder)
        label = metadata.folder_label(album.item if album else None, folder)
        entries.append(
            (
                paths.music_dir(key, library, folder),
                xbmcgui.ListItem(label, offscreen=True),
                True,
            )
        )
    _listing(request, entries, "")


def _music_folder(request, store, key, library, folder):
    """One album's complete listing, in one call, or Kodi's own rows back."""
    directory = paths.music_dir(key, library, folder)
    began = time.monotonic()
    try:
        entries = _song_entries(store, key, library, folder)
    except Exception:
        LOG.exception(
            "music listing unavailable; re-listing the rows Kodi holds for %s",
            directory,
        )
        entries = _known_entries(directory)
    built = time.monotonic()
    _listing(request, entries, "songs")
    LOG.debug(
        "music folder %s: %d songs, built in %.2f s, handed over in %.2f s",
        folder[-8:],
        len(entries),
        built - began,
        time.monotonic() - built,
    )


def _song_entries(store, key, library, folder):
    began = time.monotonic()
    songs = store.records(kind="Audio", library=library, parent_id=folder)
    album = None
    if not folder.startswith(paths.SINGLES):
        found = store.records(kind="MusicAlbum", item_ids=[folder])
        album = found[folder].item if folder in found else None
    LOG.debug(
        "music folder %s: %d songs read in %.2f s",
        folder[-8:],
        len(songs),
        time.monotonic() - began,
    )
    entries = []
    for item_id, record in sorted(songs.items()):
        url = paths.song_url(
            key, library, folder, item_id, paths.container_of(record.item)
        )
        try:
            li = metadata.song_listitem(record.item, album)
        except Exception:
            # One malformed payload must not cost the directory a song.
            LOG.exception("song row reduced to its title: %s", item_id)
            li = metadata.fallback_song_listitem(record.item)
        entries.append((url, li, False))
    return entries


def _known_entries(directory):
    """What Kodi already holds for the directory, as a listing it will keep.

    The tags are Kodi's own readback, the ids and play counts survive by
    file name, and the next pass finds the directory's hash moved and lists
    it properly. Only a store and an RPC failing together lose a song.
    """
    reply = kodirpc.call(
        "AudioLibrary.GetSongs",
        {
            "properties": [
                "file",
                "title",
                "artist",
                "albumartist",
                "album",
                "genre",
                "track",
                "disc",
                "duration",
                "year",
                "musicbrainztrackid",
                "musicbrainzalbumid",
            ],
            "filter": {"field": "path", "operator": "startswith", "value": directory},
        },
    )
    rows = reply.get("songs", []) if isinstance(reply, dict) else []
    entries = []
    for row in rows:
        if not row.get("file", "").startswith(directory):
            continue
        li = xbmcgui.ListItem(row.get("title") or row.get("label", ""), offscreen=True)
        tag = li.getMusicInfoTag()
        tag.setMediaType("song")
        tag.setTitle(row.get("title") or row.get("label", ""))
        if row.get("album"):
            tag.setAlbum(row["album"])
        if row.get("albumartist"):
            tag.setAlbumArtist(" / ".join(row["albumartist"]))
        if row.get("artist"):
            tag.setArtist(" / ".join(row["artist"]))
        if row.get("genre"):
            tag.setGenres(list(row["genre"]))
        if row.get("track"):
            tag.setTrack(int(row["track"]))
        if row.get("disc"):
            tag.setDisc(int(row["disc"]))
        if row.get("duration"):
            tag.setDuration(int(row["duration"]))
        if row.get("year"):
            tag.setYear(int(row["year"]))
        if row.get("musicbrainztrackid"):
            tag.setMusicBrainzTrackID(row["musicbrainztrackid"])
        if row.get("musicbrainzalbumid"):
            tag.setMusicBrainzAlbumID(row["musicbrainzalbumid"])
        metadata.mark_loaded(li, 0)
        entries.append((row["file"], li, False))
    return entries


# -- the scanner hold ----------------------------------------------------------


def _hold(request, scanner):
    """Keep the scanner's listing of the hold directory open while the pass
    writes, then list it empty.

    Kodi marks every library announcement made while its scanner is busy as
    a transaction, and the home widgets skip those; without the hold each
    song written re-ran four whole-library widget queries and the next
    write waited behind them (measured: 46 ms a song against 0.4 ms). The
    token the service set is the one this listing waits on; a different or
    cleared token ends it, and so does the limit.
    """
    token = state.native_hold(scanner)
    monitor = xbmc.Monitor()
    deadline = time.monotonic() + HOLD_LIMIT
    while token and state.native_hold(scanner) == token:
        if time.monotonic() >= deadline or monitor.waitForAbort(0.25):
            break
    _listing(request, [], "")
