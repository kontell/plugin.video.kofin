# -*- coding: utf-8 -*-
"""One-way materialization of Jellyfin music playlists as native Kodi basic
playlists under ``special://profile/playlists/music/Kofin/``.

Download each Audio playlist, rewrite track lines to the same path already
stored for that song in MyMusic, write one ``.m3u8`` per playlist named after
the playlist's title (``_unique_stem``, no ``kofin`` prefix). The folder is
the ownership boundary — never touch sibling files under ``playlists/music/``.

**The line has to be one Kodi can trace back to the song row**, and which line
that is depends on how the row was written — so it is decided per row, not from
the setting that produced it (a ``musicTranscode`` flip changes new rows only,
which is exactly how two installs on the same settings ended up with different
path forms).

*Plugin rows* (``musicTranscode`` on) carry the song's own MyMusic
``path.strPath + song.strFileName``, which is what Kodi itself writes when it
saves a playlist of library songs (verified live: Kodi's own ``Save`` wrote the
same ``plugin://…/stream.flac?mode=play&id=…&dbid=…`` rows kofin writes).
Playing one runs the play route, which resolves the stream, stamps the song's
tag and database id on the resolved item and reports the playback.

``musicdb://songs/<id><ext>`` cannot be used for those rows:
``CMusicDatabaseFile`` re-opens the translated path at the file layer with no
plugin resolution, so every line fails at "Init: Error opening file
musicdb://songs/<id>.mp3" before playback starts (verified live, both from the
GUI and ``Player.Open``).

*Direct rows* (``musicTranscode`` off) are the other way round, and the raw
path is the one that cannot work. Kodi does not match a bare
``https://…/Audio/<id>/stream.flac?static=true`` back to its song row — played
by path it comes back with no database id, no title, artist or album, only the
``#EXTINF`` label (verified live on Piers: the same track opened by ``songid``
answers with all of it, opened by ``file`` with none of it). Without the
database id nothing identifies the item, so the service cannot claim it either
and the playback never reaches the Jellyfin dashboard. ``musicdb://`` is what
those rows take: ``GetSongByFileName`` reads the id straight out of the URL,
and the file layer opens the direct path underneath with nothing to resolve.
"""

from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as etree
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

import xbmcvfs

from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.db import Database
from kofin.sync.fields import reference_checksum
from kofin.sync.kodidb import Music as MusicKodiDb
from kofin.sync.kodidb import queries as video_queries
from kofin.sync.nodes import fs

LOG = Logger(__name__)

FOLDER_NAME = "Kofin"
PLAYLISTS_MUSIC = "special://profile/playlists/music"
PAGE_SIZE = 100

# How a managed playlist folder gets the addon's own icon instead of Kodi's
# generic folder glyph. Both halves were measured on Piers (Kodi 22), in the
# music and the video playlist windows alike:
#
# * The *name* is not ours to choose. Kodi only looks for the names in its
#   art lists (advancedsettings ``<musicthumbs>``, and the video equivalent),
#   and ``folder.jpg`` is in them where ``folder.png`` is not — a folder.png
#   drew the plain glyph, nothing else.
# * The *content* is ours. Kodi picks the decoder off the bytes, not off the
#   extension, so the addon's own PNG goes in under the .jpg name and keeps
#   its transparency. It has to: kofin-node.png is grey+alpha, and a real
#   JPEG of it renders as the glyph on a black tile.
#
# Nothing extra ships for this — the file is a copy of the icon the node tree
# already uses (views.NODE_ROOT_ICON), made when the folder is written.
#
# One caveat, also measured: Kodi resolves a folder's art while building the
# listing that holds it, and keeps that listing for the session. An icon
# written into a folder Kodi has *already* shown appears the next time the
# listing is built cold — a Container.Refresh did not do it (Piers), a first
# visit in a later session did (Omega) — so in the worst case, the next start.
# The poll that writes the icon never shows it. New installs never notice: the
# folder and the icon are written together, before anything has listed either.
FOLDER_ICON = "folder.jpg"
FOLDER_ICON_SOURCE = "kofin-node.png"

# The Downloaded-music view (plan W3.3): a smart playlist whose one rule is
# "path starts with the downloads music root" — repointed songs' paths live
# there, so this is a real library view with no schema tricks. It sits in the
# managed folder for the addon icon, but it is kofin *state*, not a mirror of
# a server playlist: the pruner and the cleanup below both leave it (and the
# folder icon with it), or it would survive exactly one poll — the documented
# pruner behavior this exemption exists for.
DOWNLOADED_MUSIC_XSP = "Downloaded music.xsp"

# What tells a plugin row from a direct one (writers/music.py writes one or the
# other into path.strPath, per the musicTranscode setting at sync time).
PLUGIN_PREFIX = "plugin://"

VIDEO_VIEW_TYPES = frozenset({"movies", "tvshows", "musicvideos"})
AUDIO_ITEM_TYPES = frozenset({"Audio"})
VIDEO_ITEM_TYPES = frozenset(
    {"Movie", "Episode", "MusicVideo", "Video", "Trailer", "TvChannel"}
)
VIDEO_MEDIA_TYPES = frozenset({"movie", "episode", "musicvideo"})
_VIDEO_ROW_QUERY = {
    "movie": video_queries.get_movie_playlist_row,
    "episode": video_queries.get_episode_playlist_row,
    "musicvideo": video_queries.get_musicvideo_playlist_row,
}

# A stop for a server that over-reports ``TotalRecordCount`` and re-emits
# earlier rows on later pages (seen live on the playlist *list* query — see
# ``Api.music_playlists``). Paging ends on a short page, so this only bites
# when full pages keep coming; a playlist longer than this is not real.
MAX_PLAYLIST_ITEMS = 20000

# Characters the filesystem or playlist path cannot carry. Keep the server
# name otherwise intact (Unicode allowed).
_UNSAFE = re.compile(r'[/\\<>:"|?*\x00-\x1f]')

# Kodi packs the disc number into the high half of iTrack.
_TRACK_MASK = 0xFFFF


class Entry(NamedTuple):
    """One playlist line, with what Kodi needs to render it before playback.

    ``path`` is the MyMusic path; the rest fills the ``#EXTINF`` header so the
    file reads correctly on its own (Kodi replaces the label from the song row
    once the list resolves, but a playlist that has to state a duration —
    "Total duration" before anything is queued — has one).
    """

    path: str
    title: str
    artist: str = ""
    track: int = 0
    duration: int = 0


def managed_dir(root: Optional[str] = None) -> str:
    """Absolute path to the managed playlist folder."""
    if root is not None:
        return root
    base = xbmcvfs.translatePath(PLAYLISTS_MUSIC)
    return os.path.join(base, FOLDER_NAME)


def _icon_source() -> str:
    """Absolute path to the shipped icon, or '' when the addon path is unknown.

    Same defensive posture as ``browse._addon_media``: a missing icon costs a
    folder glyph, and must never cost a playlist refresh.
    """
    try:
        path = settings.addon_path()
    except Exception:  # pragma: no cover - defensive
        return ""
    if not path:
        return ""
    return os.path.join(path, "resources", "media", FOLDER_ICON_SOURCE)


def write_folder_icon(directory: str) -> bool:
    """Put :data:`FOLDER_ICON` in a managed folder. True when it was written.

    Skipped when the file is already the right size, because this runs on the
    playlist poll: rewriting it every pass would churn the folder's mtime for
    nothing (see :func:`_write_text`). Size is the cheap proxy for "the same
    icon" — the only way it changes is a new addon version shipping a new one.
    """
    source = _icon_source()
    if not source or not os.path.isfile(source):
        return False
    target = os.path.join(directory, FOLDER_ICON)
    try:
        if os.path.isfile(target) and os.path.getsize(target) == os.path.getsize(
            source
        ):
            return False
        shutil.copyfile(source, target)
    except OSError:
        LOG.exception("failed to write the playlist folder icon to %s", directory)
        return False
    return True


def downloaded_music_xsp(music_root: str) -> str:
    """The .xsp document for the Downloaded-music view (plan W3.3)."""
    from xml.sax.saxutils import escape

    try:
        name = settings.localized(30736) or "Downloaded music"
    except Exception:  # pragma: no cover - string cache misses etc.
        name = "Downloaded music"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<smartplaylist type="songs">\n'
        "    <name>%s</name>\n"
        "    <match>all</match>\n"
        '    <rule field="path" operator="startswith">\n'
        "        <value>%s</value>\n"
        "    </rule>\n"
        '    <order direction="ascending">artist</order>\n'
        "</smartplaylist>\n"
    ) % (escape(name), escape(music_root))


def refresh_downloaded_music(root: Optional[str] = None) -> bool:
    """Write or update the Downloaded-music smart playlist; True if written.

    Idempotent through ``_write_text``'s already-says-this skip, so the
    manager can call it on every finished song and the ``downloadsPath``
    apply-hook on every change — the rule's path value is the only content
    that moves.
    """
    from kofin.downloads import downloads_root
    from kofin.downloads.files import MUSIC_DIR

    directory = managed_dir(root)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        LOG.exception("cannot create the managed playlist folder %s", directory)
        return False
    write_folder_icon(directory)
    music_root = "%s/%s/" % (downloads_root().rstrip("/"), MUSIC_DIR)
    return _write_text(
        os.path.join(directory, DOWNLOADED_MUSIC_XSP), downloaded_music_xsp(music_root)
    )


def safe_filename(name: str) -> str:
    """File stem from a Jellyfin playlist name (extension added by caller)."""
    cleaned = _UNSAFE.sub("_", (name or "").strip())
    cleaned = cleaned.rstrip(" .")
    return cleaned or "playlist"


def playlist_line(kodi_id: int, str_path: str, str_filename: str) -> str:
    """The path to write for a song row: its own, or the musicdb URL for it.

    Which one is read off the row rather than off ``musicTranscode``: the row
    is what the line has to agree with, and the setting can have moved since it
    was written (see the module docstring for why each form only works for the
    rows it belongs to).

    The extension is load-bearing — ``CMusicDatabaseFile::TranslateUrl`` checks
    it against the row and refuses the id when it disagrees — so a direct row
    whose filename carries none keeps its own path. That is no worse than
    today, and better than a line that cannot open.
    """
    if str_path.startswith(PLUGIN_PREFIX):
        return join_song_path(str_path, str_filename)
    extension = os.path.splitext(str_filename.split("?", 1)[0])[1]
    if not extension:
        return join_song_path(str_path, str_filename)
    return "musicdb://songs/%d%s" % (kodi_id, extension)


def join_song_path(str_path: str, str_filename: str) -> str:
    """Rebuild the playable path the way Kodi joins path + filename."""
    path = str_path or ""
    filename = str_filename or ""
    if path and not path.endswith(("/", "\\")):
        # Direct and plugin paths from kofin always end with /; tolerate missing.
        return path + "/" + filename
    return path + filename


def entry_label(entry: Entry) -> str:
    """The ``#EXTINF`` label Kodi writes for a library song: ``NN. Artist - Title``."""
    label = entry.title or ""
    if entry.artist:
        label = "%s - %s" % (entry.artist, label) if label else entry.artist
    if entry.track:
        label = "%02d. %s" % (entry.track, label) if label else "%02d." % entry.track
    return label


def render_m3u8(entries: Iterable[Entry]) -> str:
    """Build extended m3u8 text."""
    lines = ["#EXTM3U"]
    for entry in entries:
        label = entry_label(entry).replace("\n", " ").replace("\r", "")
        lines.append("#EXTINF:%d,%s" % (entry.duration or -1, label))
        lines.append(entry.path)
    lines.append("")
    return "\n".join(lines)


def _unique_stem(name: str, taken: Set[str]) -> str:
    base = safe_filename(name)
    candidate = base
    n = 2
    while candidate.lower() in taken:
        candidate = "%s (%d)" % (base, n)
        n += 1
    taken.add(candidate.lower())
    return candidate


def enabled_kinds(views: Iterable[Any], whitelist: Set[str]) -> Set[str]:
    """Audio/Video sides that should materialize, from synced library kinds."""
    kinds: Set[str] = set()
    for view in views:
        view_id = getattr(view, "view_id", None) or (
            view.get("Id") if isinstance(view, dict) else None
        )
        media = getattr(view, "media_type", None) or (
            view.get("media_type") if isinstance(view, dict) else None
        )
        if not view_id or view_id not in whitelist:
            continue
        if media == "music":
            kinds.add("Audio")
        elif media in VIDEO_VIEW_TYPES:
            kinds.add("Video")
    return kinds


def playlist_side(media_type: str, items: Iterable[Dict[str, Any]]) -> Optional[str]:
    """Audio or Video when the playlist is homogeneous; None to skip (mixed/empty type)."""
    declared = media_type or ""
    if declared not in ("Audio", "Video"):
        return None
    seen: Set[str] = set()
    for item in items:
        item_type = item.get("Type") or ""
        if not item_type:
            continue
        if item_type in AUDIO_ITEM_TYPES:
            seen.add("Audio")
        elif item_type in VIDEO_ITEM_TYPES:
            seen.add("Video")
        else:
            seen.add(item_type)
        if len(seen) > 1:
            return None
    if seen and declared not in seen:
        return None
    return declared


class VideoPlaylistDb:
    """Path+title rows for mapped movies/episodes/musicvideos."""

    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor

    def get_playlist_row(self, kodi_id: int, media_type: str) -> Optional[Any]:
        query = _VIDEO_ROW_QUERY.get(media_type)
        if not query:
            return None
        self.cursor.execute(query, (kodi_id,))
        return self.cursor.fetchone()


def video_entry(
    mapping: jellyfin_db.JellyfinDatabase, video: Any, jellyfin_id: str
) -> Optional[Entry]:
    """The playlist line for a mapped video id, or None if unsynced."""
    row = mapping.get_item_by_id(jellyfin_id)
    if row is None or row.media_type not in VIDEO_MEDIA_TYPES:
        return None
    path_row = video.get_playlist_row(row.kodi_id, row.media_type)
    if path_row is None:
        return None
    str_path, str_filename = path_row[0], path_row[1]
    if not str_path or not str_filename:
        return None
    return Entry(
        path=join_song_path(str_path, str_filename),
        title=path_row[2] or "",
    )


def song_entry(
    mapping: jellyfin_db.JellyfinDatabase, music: MusicKodiDb, jellyfin_id: str
) -> Optional[Entry]:
    """The playlist line for a mapped Audio id, or None if unsynced."""
    row = mapping.get_item_by_id(jellyfin_id)
    if row is None or row.media_type != "song":
        return None
    song = music.get_song_playlist_row(row.kodi_id)
    if song is None:
        return None
    str_path, str_filename = song[0], song[1]
    if not str_path or not str_filename:
        return None
    return Entry(
        path=playlist_line(row.kodi_id, str_path, str_filename),
        title=song[2] or "",
        artist=song[3] or "",
        track=int(song[4] or 0) & _TRACK_MASK,
        duration=int(song[5] or 0),
    )


def _iter_playlist_items(api: Any, playlist_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start = 0
    while True:
        body = api.playlist_items(playlist_id, start_index=start, limit=PAGE_SIZE)
        page = body.get("Items") or []
        if not page:
            break
        items.extend(page)
        if len(page) < PAGE_SIZE:
            # A short page is the end of the playlist, whatever the count says.
            break
        start += len(page)
        total = int(body.get("TotalRecordCount") or 0)
        if total and start >= total:
            break
        if start >= MAX_PLAYLIST_ITEMS:
            LOG.warning(
                "playlist %s still paging at %d items; stopping",
                playlist_id,
                start,
            )
            break
    return items


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _write_text(path: str, content: str) -> bool:
    """Write the file unless it already says this. True when it was written.

    A playlist nobody edited must not churn its mtime — skins sort playlist
    folders by date, and a rewrite invalidates Kodi's directory cache.
    """
    if _read_text(path) == content:
        return False

    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    # Atomic-ish: write temp then replace so a crash mid-write leaves the old file.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp, path)
    return True


def _list_files(directory: str) -> List[str]:
    if not os.path.isdir(directory):
        return []
    return [
        name
        for name in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, name))
    ]


def cleanup_managed_playlists(root: Optional[str] = None) -> int:
    """Remove the managed ``Kofin/`` folder (or empty it). Returns files removed.

    The Downloaded-music view survives the sweep (with the folder icon, which
    exists for whatever remains): this cleanup is "stop mirroring the server's
    playlists", and the downloads view mirrors nothing — turning playlist sync
    off must not take the offline library's music view with it.
    """
    directory = managed_dir(root)
    if not os.path.isdir(directory):
        return 0
    names = _list_files(directory)
    keep = {name for name in names if name == DOWNLOADED_MUSIC_XSP}
    if keep:
        keep.add(FOLDER_ICON)
    removed = 0
    for name in names:
        if name in keep:
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            LOG.exception("failed to remove managed playlist %s", name)
    try:
        # Only remove the directory if empty (ignore leftover junk).
        if not os.listdir(directory):
            os.rmdir(directory)
    except OSError:
        pass
    LOG.info("music playlists: cleaned %d managed file(s)", removed)
    return removed


def refresh_music_playlists(
    api: Any,
    mapping: jellyfin_db.JellyfinDatabase,
    music: MusicKodiDb,
    root: Optional[str] = None,
) -> Dict[str, int]:
    """Download all music playlists and rewrite the managed folder.

    Returns counts: playlists, written, tracks, skipped, pruned.
    """
    directory = managed_dir(root)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    write_folder_icon(directory)

    playlists = api.music_playlists()

    # An empty listing against existing files is not a deletion order. This
    # folder's ownership rule is the *folder* (CLAUDE.md): every managed
    # .m3u8 not in ``want`` is removed, so a listing that comes back empty
    # for any reason other than "the user has no playlists" empties the lot.
    # Permission changes, a filter, and the server contradicting its own
    # TotalRecordCount all look exactly like an empty account from here.
    # Skip and warn, mirroring boxsets.sweep_stale and the prune's
    # get_existing_ids. The cost is one skipped prune the first time a user
    # deletes their last playlist; the folder is cleared properly by
    # cleanup_managed_playlists, and by the next pass that lists at least one.
    if not playlists:
        standing = [name for name in _list_files(directory) if name.endswith(".m3u8")]
        if standing:
            LOG.warning(
                "music playlists: the server listed none while %d managed "
                "playlist(s) stand — skipping the prune rather than emptying "
                "the folder",
                len(standing),
            )
            return {
                "playlists": 0,
                "written": 0,
                "tracks": 0,
                "skipped": 0,
                "pruned": 0,
            }

    taken: Set[str] = set()
    want: Set[str] = set()
    track_total = 0
    skipped = 0
    written = 0

    for playlist in playlists:
        playlist_id = playlist.get("Id") or ""
        name = playlist.get("Name") or "playlist"
        if not playlist_id:
            continue

        entries: List[Entry] = []
        missing = 0
        for item in _iter_playlist_items(api, playlist_id):
            item_id = item.get("Id")
            # Prefer Audio items; still try mapping if Type is missing.
            item_type = item.get("Type") or ""
            if item_type and item_type != "Audio":
                missing += 1
                continue
            if not item_id:
                missing += 1
                continue
            entry = song_entry(mapping, music, item_id)
            if entry is None:
                missing += 1
                continue
            entries.append(entry)
            track_total += 1

        stem = _unique_stem(name, taken)
        filename = stem + ".m3u8"
        path = os.path.join(directory, filename)
        if _write_text(path, render_m3u8(entries)):
            written += 1
        want.add(filename)
        skipped += missing

        if missing:
            # A partial playlist is otherwise silent: the user sees a short
            # playlist and nothing says the rest is in a library they did not
            # sync (or has not been written yet).
            LOG.info(
                "music playlist %s: %d track(s), %d not in the Kodi library",
                name,
                len(entries),
                missing,
            )

    pruned = 0
    for existing in _list_files(directory):
        if existing.endswith(".tmp"):
            try:
                os.remove(os.path.join(directory, existing))
            except OSError:
                pass
            continue
        if existing in (FOLDER_ICON, DOWNLOADED_MUSIC_XSP):
            # Ours, and not server playlists: the prune is against the
            # server's set.
            continue
        if existing not in want:
            try:
                os.remove(os.path.join(directory, existing))
                pruned += 1
            except OSError:
                LOG.exception("failed to prune managed playlist %s", existing)

    stats = {
        "playlists": len(want),
        "written": written,
        "tracks": track_total,
        "skipped": skipped,
        "pruned": pruned,
    }
    LOG.info(
        "music playlists: %d playlist(s), %d rewritten, %d track(s), "
        "%d skipped, %d pruned",
        stats["playlists"],
        stats["written"],
        stats["tracks"],
        stats["skipped"],
        stats["pruned"],
    )
    return stats


def refresh_with_databases(api: Any, root: Optional[str] = None) -> Dict[str, int]:
    """Open kofin + music DBs and run :func:`refresh_music_playlists`."""
    with Database("music") as musicdb:
        with Database("kofin") as kofindb_conn:
            mapping = jellyfin_db.JellyfinDatabase(kofindb_conn.cursor)
            music = MusicKodiDb(musicdb.cursor)
            return refresh_music_playlists(api, mapping, music, root=root)


def _entries_for(
    items: List[Dict[str, Any]],
    side: str,
    mapping: jellyfin_db.JellyfinDatabase,
    music: Optional[MusicKodiDb],
    video: Optional[Any],
) -> Tuple[List[Entry], int]:
    entries: List[Entry] = []
    missing = 0
    for item in items:
        item_id = item.get("Id")
        item_type = item.get("Type") or ""
        if not item_id:
            missing += 1
            continue
        if side == "Audio":
            if item_type and item_type != "Audio":
                missing += 1
                continue
            if music is None:
                missing += 1
                continue
            entry = song_entry(mapping, music, item_id)
        else:
            if item_type and item_type not in VIDEO_ITEM_TYPES:
                missing += 1
                continue
            if video is None:
                missing += 1
                continue
            entry = video_entry(mapping, video, item_id)
        if entry is None:
            missing += 1
            continue
        entries.append(entry)
    return entries, missing


def write_playlist_file(
    directory: str,
    name: str,
    entries: Iterable[Entry],
    taken: Set[str],
) -> Tuple[str, bool]:
    """Write one .m3u8. Returns (filename, written)."""
    stem = _unique_stem(name, taken)
    filename = stem + ".m3u8"
    path = os.path.join(directory, filename)
    return filename, _write_text(path, render_m3u8(entries))


def remove_managed_file(directory: str, filename: str) -> bool:
    path = os.path.join(directory, filename)
    if not filename or not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        LOG.exception("failed to remove managed playlist %s", filename)
        return False


def apply_one(
    api: Any,
    mapping: jellyfin_db.JellyfinDatabase,
    music: Optional[MusicKodiDb],
    video: Optional[Any],
    state: jellyfin_db.JellyfinDatabase,
    playlist: Dict[str, Any],
    kinds: Set[str],
    music_root: Optional[str] = None,
    video_root: Optional[str] = None,
) -> bool:
    """Materialize one server playlist. True when a file was written or removed."""
    playlist_id = playlist.get("Id") or ""
    if not playlist_id:
        return False
    items = _iter_playlist_items(api, playlist_id)
    side = playlist_side(playlist.get("MediaType") or "", items)
    stored = state.get_playlist_state(playlist_id)
    if side is None or side not in kinds:
        if stored:
            directory = (
                managed_dir(music_root)
                if stored[0] == "Audio"
                else managed_video_dir() if video_root is None else video_root
            )
            remove_managed_file(directory, stored[1])
            state.remove_playlist_state(playlist_id)
            return True
        return False

    etag = playlist.get("Etag") or ""
    checksum = reference_checksum(etag) if etag else ""
    if stored and stored[0] == side and stored[2] == checksum:
        return False

    entries, missing = _entries_for(items, side, mapping, music, video)
    directory = (
        managed_dir(music_root)
        if side == "Audio"
        else (video_root if video_root is not None else managed_video_dir())
    )
    if not os.path.isdir(directory):
        os.makedirs(directory)
    write_folder_icon(directory)
    taken: Set[str] = set()
    if stored and stored[0] == side:
        taken.add(os.path.splitext(stored[1])[0].lower())
    filename, written = write_playlist_file(
        directory, playlist.get("Name") or "playlist", entries, taken
    )
    if stored and stored[1] != filename:
        remove_managed_file(directory, stored[1])
    state.add_playlist_state(playlist_id, side, filename, checksum)
    if missing:
        LOG.info(
            "playlist %s: %d item(s), %d not in the Kodi library",
            playlist.get("Name"),
            len(entries),
            missing,
        )
    return written or bool(stored and stored[1] != filename)


def remove_one(
    state: jellyfin_db.JellyfinDatabase,
    playlist_id: str,
    music_root: Optional[str] = None,
    video_root: Optional[str] = None,
) -> bool:
    stored = state.get_playlist_state(playlist_id)
    if not stored:
        return False
    directory = (
        managed_dir(music_root)
        if stored[0] == "Audio"
        else (video_root if video_root is not None else managed_video_dir())
    )
    removed = remove_managed_file(directory, stored[1])
    state.remove_playlist_state(playlist_id)
    return removed


def reconcile(
    api: Any,
    mapping: jellyfin_db.JellyfinDatabase,
    music: Optional[MusicKodiDb],
    video: Optional[Any],
    state: jellyfin_db.JellyfinDatabase,
    kinds: Set[str],
    music_root: Optional[str] = None,
    video_root: Optional[str] = None,
) -> Dict[str, int]:
    """Full list + Etag skip + prune for the enabled sides."""
    stats = {"playlists": 0, "written": 0, "tracks": 0, "skipped": 0, "pruned": 0}
    if not kinds:
        return stats

    listed = api.playlists() if hasattr(api, "playlists") else api.music_playlists()
    want_audio: Set[str] = set()
    want_video: Set[str] = set()
    taken_audio: Set[str] = set()
    taken_video: Set[str] = set()

    audio_dir = managed_dir(music_root)
    video_dir = video_root if video_root is not None else managed_video_dir()
    if "Audio" in kinds and not os.path.isdir(audio_dir):
        os.makedirs(audio_dir)
        write_folder_icon(audio_dir)
    if "Video" in kinds and not os.path.isdir(video_dir):
        os.makedirs(video_dir)
        write_folder_icon(video_dir)

    standing_audio = (
        [name for name in _list_files(audio_dir) if name.endswith(".m3u8")]
        if os.path.isdir(audio_dir)
        else []
    )
    standing_video = (
        [name for name in _list_files(video_dir) if name.endswith(".m3u8")]
        if os.path.isdir(video_dir)
        else []
    )

    if not listed:
        if standing_audio or standing_video:
            LOG.warning(
                "playlists: the server listed none while managed "
                "playlist(s) stand — skipping the prune rather than emptying "
                "the folder"
            )
        return stats

    for playlist in listed:
        playlist_id = playlist.get("Id") or ""
        if not playlist_id:
            continue
        items = _iter_playlist_items(api, playlist_id)
        side = playlist_side(playlist.get("MediaType") or "", items)
        if side is None or side not in kinds:
            continue
        etag = playlist.get("Etag") or ""
        checksum = reference_checksum(etag) if etag else ""
        stored = state.get_playlist_state(playlist_id)
        directory = audio_dir if side == "Audio" else video_dir
        taken = taken_audio if side == "Audio" else taken_video
        want = want_audio if side == "Audio" else want_video
        if stored and stored[0] == side and stored[2] == checksum and stored[1]:
            want.add(stored[1])
            taken.add(os.path.splitext(stored[1])[0].lower())
            stats["playlists"] += 1
            continue
        entries, missing = _entries_for(items, side, mapping, music, video)
        stats["tracks"] += len(entries)
        stats["skipped"] += missing
        filename, written = write_playlist_file(
            directory, playlist.get("Name") or "playlist", entries, taken
        )
        if stored and stored[1] != filename:
            remove_managed_file(directory, stored[1])
            stats["pruned"] += 1
        state.add_playlist_state(playlist_id, side, filename, checksum)
        want.add(filename)
        stats["playlists"] += 1
        if written:
            stats["written"] += 1
        if missing:
            LOG.info(
                "playlist %s: %d item(s), %d not in the Kodi library",
                playlist.get("Name"),
                len(entries),
                missing,
            )

    known = {row[0]: row for row in state.get_playlist_states()}
    for playlist_id, media_type, filename, _checksum in list(known.values()):
        if media_type == "Audio" and "Audio" in kinds and filename not in want_audio:
            remove_managed_file(audio_dir, filename)
            state.remove_playlist_state(playlist_id)
            stats["pruned"] += 1
        elif media_type == "Video" and "Video" in kinds and filename not in want_video:
            remove_managed_file(video_dir, filename)
            state.remove_playlist_state(playlist_id)
            stats["pruned"] += 1

    if "Audio" in kinds:
        for existing in _list_files(audio_dir):
            if existing.endswith(".tmp"):
                try:
                    os.remove(os.path.join(audio_dir, existing))
                except OSError:
                    pass
                continue
            if existing in (FOLDER_ICON, DOWNLOADED_MUSIC_XSP):
                continue
            if existing.endswith(".m3u8") and existing not in want_audio:
                try:
                    os.remove(os.path.join(audio_dir, existing))
                    stats["pruned"] += 1
                except OSError:
                    LOG.exception("failed to prune managed playlist %s", existing)
    if "Video" in kinds:
        for existing in _list_files(video_dir):
            if existing.endswith(".m3u8") and existing not in want_video:
                try:
                    os.remove(os.path.join(video_dir, existing))
                    stats["pruned"] += 1
                except OSError:
                    LOG.exception("failed to prune managed playlist %s", existing)

    LOG.info(
        "playlists: %d playlist(s), %d rewritten, %d track(s), "
        "%d skipped, %d pruned",
        stats["playlists"],
        stats["written"],
        stats["tracks"],
        stats["skipped"],
        stats["pruned"],
    )
    return stats


def cleanup_video_playlists(root: Optional[str] = None) -> int:
    """Remove managed Jellyfin video .m3u8 files from the video Kofin folder.

    Library smart playlists (.xsp) and the folder icon stay.
    """
    directory = root if root is not None else managed_video_dir()
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in _list_files(directory):
        if not name.endswith(".m3u8"):
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            LOG.exception("failed to remove managed video playlist %s", name)
    return removed


def parse_m3u_paths(text: str) -> List[str]:
    """Ordered file lines from an extended m3u, comments skipped."""
    paths: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        paths.append(line)
    return paths


def jellyfin_id_from_path(path: str, mapping: Any) -> Optional[str]:
    """Jellyfin id encoded in a playlist line, or via kofin.db for musicdb/videodb."""
    if not path:
        return None
    match = re.search(r"[?&]id=([0-9a-fA-F]{32})", path)
    if match:
        return match.group(1).lower()
    match = re.search(r"/(?:Audio|Videos)/([0-9a-fA-F]{32})(?:/|$)", path)
    if match:
        return match.group(1).lower()
    match = re.search(r"musicdb://songs/(\d+)", path)
    if match and mapping is not None:
        kodi_id = int(match.group(1))
        found = mapping.get_item_by_kodi_id(kodi_id, "song")
        return found if isinstance(found, str) else None
    match = re.search(r"videodb://movies/titles/(\d+)", path)
    if match and mapping is not None:
        kodi_id = int(match.group(1))
        found = mapping.get_item_by_kodi_id(kodi_id, "movie")
        return found if isinstance(found, str) else None
    return None


# --- the video smart playlists (P2.1: moved here from views.py) -----------------
#
# One .xsp per synced video library, in a managed folder under Kodi's own
# video playlists. The folder holds only generated files, so there is no
# user ordering or label to preserve; its icon is the only thing that can
# carry the addon's art (a .tbn beside an .xsp does nothing, measured on
# Piers). Written whole every pass, like the nodes: a leftover tag rule
# for a renamed library matched nothing under match=all.
#
# Ownership differs from the music side above: here the ``kofin`` prefix
# gates every deletion and a foreign file in the folder is spared
# (CLAUDE.md), because the folder used to be the user's own directory.

PLAYLIST_KINDS = ("movies", "tvshows", "musicvideos")


def video_playlists_dir() -> str:
    """Kodi's own video playlist directory -- the user's, not ours."""
    return xbmcvfs.translatePath("special://profile/playlists/video")


def managed_video_dir() -> str:
    """The managed folder inside it -- ours."""
    return os.path.join(video_playlists_dir(), FOLDER_NAME)


def video_playlist_file(view: Dict[str, Any]) -> str:
    return "%s%s%s.xsp" % (fs.PREFIX, view["Media"], view["Id"])


def build_video_playlist(view: Dict[str, Any], mixed: bool = False) -> etree.Element:
    xml = etree.Element("smartplaylist", {"type": view["Media"]})
    etree.SubElement(xml, "name").text = (
        view["Name"] if not mixed else "%s (%s)" % (view["Name"], view["Media"])
    )
    etree.SubElement(xml, "match").text = "all"
    rule = etree.SubElement(xml, "rule", {"field": "tag", "operator": "is"})
    etree.SubElement(rule, "value").text = view["Tag"]
    return xml


def write_video_playlists(entries: Iterable[Any]) -> None:
    """One playlist per video-kind entry; the folder, its icon, and a
    reconcile of whatever else kofin left in there."""
    directory = managed_video_dir()

    if not os.path.isdir(directory):
        os.makedirs(directory)

    write_folder_icon(directory)
    keep = set()

    for view, mixed in entries:
        if view["Media"] not in PLAYLIST_KINDS:
            continue
        name = video_playlist_file(view)
        etree.ElementTree(build_video_playlist(view, mixed)).write(
            os.path.join(directory, name)
        )
        keep.add(name)

    for name in fs.remove_managed_entries(directory, keep=keep, label="playlist"):
        LOG.info("--[ playlists ] pruned stale %s", name)


def remove_video_playlists() -> None:
    """Remove every generated playlist, the managed folder with them.

    The generated files and the folder's own icon go, the folder once it is
    empty; anything else in there is not ours to remove.
    """
    directory = managed_video_dir()
    fs.remove_managed_entries(directory, also=(FOLDER_ICON,), label="playlist")
    fs.remove_empty(directory)
    migrate_flat_video_playlists()


def remove_video_playlist_for(view_id: str) -> None:
    """Remove one library's playlist from either home: a library removed
    between the upgrade and the next generation still has its playlist out
    in the old flat layout."""
    for directory in (managed_video_dir(), video_playlists_dir()):
        _, files = fs.listdir(directory)
        for name in files:
            if fs.is_managed(name) and name.endswith("%s.xsp" % view_id):
                fs.delete_file(os.path.join(directory, name), "playlist")


def migrate_flat_video_playlists() -> None:
    """Clear out the pre-folder layout.

    The generated smart playlists used to sit directly in the user's
    ``playlists/video/``. They are regenerated inside the managed folder, so
    the old copies are dead weight -- and, being smart playlists over the
    same tag, would show up twice under two names. This sweeps the user's
    directory, so only a generated ``kofin*.xsp`` qualifies; the managed
    folder is a directory and is never touched here.
    """
    directory = video_playlists_dir()
    _, files = fs.listdir(directory)
    for name in files:
        if fs.is_managed(name) and name.endswith(".xsp"):
            fs.delete_file(os.path.join(directory, name), "playlist")
