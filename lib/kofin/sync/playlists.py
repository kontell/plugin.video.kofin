# -*- coding: utf-8 -*-
"""One-way materialization of Jellyfin music playlists as native Kodi basic
playlists under ``special://profile/playlists/music/Kofin/``.

Download each Audio playlist, rewrite track lines to the same path already
stored for that song in MyMusic, write ``<Server Name>.m3u8``. The folder is
the ownership boundary — never touch sibling files under ``playlists/music/``.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import xbmcvfs

from kofin.core.log import Logger
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.db import Database
from kofin.sync.kodidb import Music as MusicKodiDb

LOG = Logger(__name__)

FOLDER_NAME = "Kofin"
PLAYLISTS_MUSIC = "special://profile/playlists/music"
PAGE_SIZE = 100

# Characters the filesystem or playlist path cannot carry. Keep the server
# name otherwise intact (Unicode allowed).
_UNSAFE = re.compile(r'[/\\<>:"|?*\x00-\x1f]')


def managed_dir(root: Optional[str] = None) -> str:
    """Absolute path to the managed playlist folder."""
    if root is not None:
        return root
    base = xbmcvfs.translatePath(PLAYLISTS_MUSIC)
    return os.path.join(base, FOLDER_NAME)


def safe_filename(name: str) -> str:
    """File stem from a Jellyfin playlist name (extension added by caller)."""
    cleaned = _UNSAFE.sub("_", (name or "").strip())
    cleaned = cleaned.rstrip(" .")
    return cleaned or "playlist"


def join_song_path(str_path: str, str_filename: str) -> str:
    """Rebuild the playable path the way Kodi joins path + filename."""
    path = str_path or ""
    filename = str_filename or ""
    if path and not path.endswith(("/", "\\")):
        # Direct and plugin paths from kofin always end with /; tolerate missing.
        return path + "/" + filename
    return path + filename


def render_m3u8(entries: Iterable[Tuple[str, str]]) -> str:
    """Build extended m3u8 text. Each entry is ``(title, play_path)``."""
    lines = ["#EXTM3U"]
    for title, play_path in entries:
        safe_title = (title or "").replace("\n", " ").replace("\r", "")
        lines.append("#EXTINF:-1,%s" % safe_title)
        lines.append(play_path)
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


def song_play_path(
    mapping: jellyfin_db.JellyfinDatabase, music: MusicKodiDb, jellyfin_id: str
) -> Optional[Tuple[str, str]]:
    """Return ``(play_path, title)`` for a mapped Audio id, or None if unsynced."""
    row = mapping.get_item_by_id(jellyfin_id)
    if row is None or row.media_type != "song":
        return None
    path_row = music.get_song_path_filename(row.kodi_id)
    if path_row is None:
        return None
    str_path, str_filename, title = path_row[0], path_row[1], path_row[2]
    if not str_path or not str_filename:
        return None
    return join_song_path(str_path, str_filename), title or ""


def _iter_playlist_items(api: Any, playlist_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start = 0
    while True:
        body = api.playlist_items(playlist_id, start_index=start, limit=PAGE_SIZE)
        page = body.get("Items") or []
        items.extend(page)
        total = int(body.get("TotalRecordCount") or 0)
        start += len(page)
        if not page or start >= total:
            break
    return items


def _write_text(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    # Atomic-ish: write temp then replace so a crash mid-write leaves the old file.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp, path)


def _list_files(directory: str) -> List[str]:
    if not os.path.isdir(directory):
        return []
    return [
        name
        for name in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, name))
    ]


def cleanup_managed_playlists(root: Optional[str] = None) -> int:
    """Remove the managed ``Kofin/`` folder (or empty it). Returns files removed."""
    directory = managed_dir(root)
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in _list_files(directory):
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

    Returns counts: playlists, tracks, skipped, pruned.
    """
    directory = managed_dir(root)
    if not os.path.isdir(directory):
        os.makedirs(directory)

    playlists = api.music_playlists()
    taken: Set[str] = set()
    want: Set[str] = set()
    track_total = 0
    skipped = 0

    for playlist in playlists:
        playlist_id = playlist.get("Id") or ""
        name = playlist.get("Name") or "playlist"
        if not playlist_id:
            continue

        entries: List[Tuple[str, str]] = []
        for item in _iter_playlist_items(api, playlist_id):
            item_id = item.get("Id")
            # Prefer Audio items; still try mapping if Type is missing.
            item_type = item.get("Type") or ""
            if item_type and item_type != "Audio":
                skipped += 1
                continue
            if not item_id:
                skipped += 1
                continue
            resolved = song_play_path(mapping, music, item_id)
            if resolved is None:
                skipped += 1
                continue
            play_path, title = resolved
            entries.append((title or item.get("Name") or "", play_path))
            track_total += 1

        stem = _unique_stem(name, taken)
        filename = stem + ".m3u8"
        path = os.path.join(directory, filename)
        _write_text(path, render_m3u8(entries))
        want.add(filename)

    pruned = 0
    for existing in _list_files(directory):
        if existing.endswith(".tmp"):
            try:
                os.remove(os.path.join(directory, existing))
            except OSError:
                pass
            continue
        if existing not in want:
            try:
                os.remove(os.path.join(directory, existing))
                pruned += 1
            except OSError:
                LOG.exception("failed to prune managed playlist %s", existing)

    stats = {
        "playlists": len(want),
        "tracks": track_total,
        "skipped": skipped,
        "pruned": pruned,
    }
    LOG.info(
        "music playlists: %d playlist(s), %d track(s), %d skipped, %d pruned",
        stats["playlists"],
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
