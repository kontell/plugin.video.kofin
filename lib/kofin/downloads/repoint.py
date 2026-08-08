"""Repoint policy: which rows move where, and the restore capture (W1.7/W1.8).

The mechanical layer is :mod:`kofin.sync.kodidb.downloads`; this module owns
the policy: the mapping lookup, directory math from the stored ``rel_path``,
when the writer-built filename is captured for restore, the prune order, and
the downloads tag on the Kodi rows.

Everything here is idempotent on purpose. "Already repointed" is the common
case, not an error: the writers re-assert a downloaded item's repoint at the
end of their own pass (``reassert_on``, plan W1.8 — inside their transaction,
which is why every entry point has an ``_on`` variant taking the caller's
cursors; a second connection would sit on the WAL write lock the writer
holds), and the manager re-runs it at start for drift — a library repair
regenerates every row in writer shape, and the next reconcile walks the done
downloads back onto their files.

Restore is a put-back, not a re-derivation: ``restore_filename`` is the
files row's content as the writers last built it, captured verbatim at
repoint time and re-captured whenever a writer pass rebuilds the row — so a
server-side rename never restores a stale URL, a transcode (whose local name
shares nothing with the server's) restores exactly, and the L2 suite can
hold restore to byte-identical.
"""

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from kofin.core.log import Logger
from kofin.downloads import TAG, downloads_root, store
from kofin.sync.db import Database
from kofin.sync.kodidb.downloads import Downloads as KodiDownloads

LOG = Logger(__name__)

REPOINTABLE = ("movie", "episode")


@dataclass
class Mapping:
    kodi_id: int
    kodi_fileid: int
    kodi_pathid: int
    media_type: str


def mapping_for_on(kofin_cursor: Any, jellyfin_id: str) -> Optional[Mapping]:
    """The item's Kodi ids out of the kofin.db mapping, None when unmapped.

    A row without a file id cannot be repointed (a show, a season, an item
    the writers never landed) and answers None too.
    """
    kofin_cursor.execute(
        "SELECT kodi_id, kodi_fileid, kodi_pathid, media_type "
        "FROM jellyfin WHERE jellyfin_id = ?",
        (jellyfin_id,),
    )
    row = kofin_cursor.fetchone()
    if row is None or row[0] is None or row[1] is None or row[2] is None:
        return None
    return Mapping(int(row[0]), int(row[1]), int(row[2]), str(row[3] or ""))


def _directory_chain(root: str, rel_path: str) -> Tuple[List[str], str]:
    """(absolute directory chain top-down, filename) for a stored rel_path."""
    parts = [part for part in rel_path.split("/") if part]
    if len(parts) < 2:
        return [], parts[0] if parts else ""
    chain = []
    current = root.rstrip("/")
    for part in parts[:-1]:
        current = "%s/%s" % (current, part)
        chain.append(current)
    return chain, parts[-1]


def _valid_chain(media_type: str, chain: List[str]) -> bool:
    if media_type == "movie":
        return len(chain) == 2  # Movies/<Title (Year)>
    return len(chain) in (2, 3)  # TV/<Show>[/Season NN]


def repoint(download: store.Download, root: str) -> bool:
    with Database("kofin") as kofin_db, Database("video") as video:
        return repoint_on(video.cursor, kofin_db.cursor, download, root)


def restore(download: store.Download, root: str) -> bool:
    with Database("kofin") as kofin_db, Database("video") as video:
        return restore_on(video.cursor, kofin_db.cursor, download, root)


def repoint_on(
    video_cursor: Any, kofin_cursor: Any, download: store.Download, root: str
) -> bool:
    """Point the item's library rows at the downloaded file; False = untouched.

    Captures the writers' current ``strFilename`` into the download row
    before moving anything, whenever the row is not already ours — which is
    both the first repoint and every re-assert after a writer pass rebuilt
    the row.
    """
    mapping = mapping_for_on(kofin_cursor, download.jellyfin_id)
    if mapping is None or mapping.media_type not in REPOINTABLE:
        LOG.warning(
            "repoint skipped for %s: no usable mapping (%s)",
            download.jellyfin_id,
            mapping.media_type if mapping else "unmapped",
        )
        return False
    chain, filename = _directory_chain(root, download.rel_path)
    if not filename or not _valid_chain(mapping.media_type, chain):
        LOG.warning(
            "repoint skipped for %s: unusable rel_path %r",
            download.jellyfin_id,
            download.rel_path,
        )
        return False

    kodi = KodiDownloads(video_cursor)
    location = kodi.file_location(mapping.kodi_fileid)
    if location is None:
        LOG.warning(
            "repoint skipped for %s: file row %s is gone",
            download.jellyfin_id,
            mapping.kodi_fileid,
        )
        return False
    _current_path_id, current_name = location
    if current_name and current_name != filename:
        store.set_restore_filename_on(kofin_cursor, download.jellyfin_id, current_name)
    if mapping.media_type == "movie":
        target = kodi.ensure_movie_paths(chain[0], chain[1])
    else:
        season_dir = chain[2] if len(chain) == 3 else None
        target = kodi.ensure_episode_paths(chain[0], chain[1], season_dir)
    kodi.set_file_location(mapping.kodi_fileid, target, filename)
    if mapping.media_type == "episode":
        kodi.set_episode_location(
            mapping.kodi_id, "%s/%s" % (chain[-1], filename), target
        )
    LOG.info(
        "repointed %s (%s %s) at %s",
        download.jellyfin_id,
        mapping.media_type,
        mapping.kodi_id,
        download.rel_path,
    )
    return True


def restore_on(
    video_cursor: Any, kofin_cursor: Any, download: store.Download, root: str
) -> bool:
    """Put the writers' rows back and prune the directory rows; False = left.

    Refuses without a captured ``restore_filename``: inventing a plugin URL
    here would be the re-derivation this design rejects, and a done download
    that never captured one does not exist in the normal flow.
    """
    mapping = mapping_for_on(kofin_cursor, download.jellyfin_id)
    if mapping is None or mapping.media_type not in REPOINTABLE:
        LOG.warning("restore skipped for %s: no usable mapping", download.jellyfin_id)
        return False
    if not download.restore_filename:
        LOG.error("restore refused for %s: no captured filename", download.jellyfin_id)
        return False
    chain, _filename = _directory_chain(root, download.rel_path)

    kodi = KodiDownloads(video_cursor)
    kodi.set_file_location(
        mapping.kodi_fileid, mapping.kodi_pathid, download.restore_filename
    )
    if mapping.media_type == "episode":
        kodi.set_episode_location(
            mapping.kodi_id, download.restore_filename, mapping.kodi_pathid
        )
    kodi.prune_paths(list(reversed(chain)))
    LOG.info(
        "restored %s (%s %s) to its plugin path",
        download.jellyfin_id,
        mapping.media_type,
        mapping.kodi_id,
    )
    return True


def reassert_on(video_cursor: Any, kofin_cursor: Any, jellyfin_id: str) -> None:
    """The writers' post-pass hook (plan W1.8): a changed item's rewrite put
    the row back in writer shape moments ago inside this same transaction —
    recapture the fresh URL and repoint again. Both happen or neither is
    visible: the writer commits per page, so the transient writer state never
    reaches a reader. No-op for anything not downloaded."""
    row = store.get_on(kofin_cursor, jellyfin_id)
    if row is None or row.state != store.DONE:
        return
    repoint_on(video_cursor, kofin_cursor, row, downloads_root())
    # The badge rides along: a repair rebuilds the item under a new Kodi id
    # and the art rows went with the old one, so re-publishing here is what
    # keeps the badge true across the most destructive resync there is.
    stamp_badge_on(video_cursor, kofin_cursor, row)


def _kodi_id_on(kofin_cursor: Any, jellyfin_id: str, media_type: str) -> Optional[int]:
    """A bare kodi id lookup — unlike ``mapping_for_on`` it asks for no file
    id, because the tag surface for episodes is their *show*, and shows have
    no file row to demand."""
    kofin_cursor.execute(
        "SELECT kodi_id FROM jellyfin WHERE jellyfin_id = ? AND media_type = ?",
        (jellyfin_id, media_type),
    )
    row = kofin_cursor.fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def _tag_target(
    kofin_cursor: Any, download: store.Download
) -> Optional[Tuple[int, str]]:
    """(kodi id, media type) of the row the downloads tag lives on."""
    if download.media_type == "movie":
        kodi_id = _kodi_id_on(kofin_cursor, download.jellyfin_id, "movie")
        return (kodi_id, "movie") if kodi_id is not None else None
    if download.media_type == "episode" and download.series_id:
        kodi_id = _kodi_id_on(kofin_cursor, download.series_id, "tvshow")
        return (kodi_id, "tvshow") if kodi_id is not None else None
    return None


BADGE_URL = "special://home/addons/plugin.video.kofin/resources/media/downloaded.png"


def _badge_target(
    kofin_cursor: Any, download: store.Download
) -> Optional[Tuple[int, str]]:
    """(kodi id, media type) the badge belongs on.

    Unlike the tag, this is the *item itself* for both kinds: an episode
    row carries its own art in a native list, so the badge lands where the
    viewer is looking rather than on the show.
    """
    kodi_id = _kodi_id_on(kofin_cursor, download.jellyfin_id, download.media_type)
    if kodi_id is None or download.media_type not in REPOINTABLE:
        return None
    return kodi_id, download.media_type


def stamp_badge_on(
    video_cursor: Any, kofin_cursor: Any, download: store.Download
) -> None:
    """Publish the downloaded badge for this item (idempotent)."""
    target = _badge_target(kofin_cursor, download)
    if target is not None:
        KodiDownloads(video_cursor).set_badge(target[0], target[1], BADGE_URL)


def stamp_badge(download: store.Download) -> None:
    with Database("kofin") as kofin_db, Database("video") as video:
        stamp_badge_on(video.cursor, kofin_db.cursor, download)


def clear_badge(download: store.Download) -> None:
    with Database("kofin") as kofin_db, Database("video") as video:
        target = _badge_target(kofin_db.cursor, download)
        if target is not None:
            KodiDownloads(video.cursor).clear_badge(target[0], target[1])


def stamp_tag(download: store.Download) -> None:
    """The downloads tag onto the Kodi row the moment a download lands.

    Movies carry it themselves; an episode tags its *show* (episode rows have
    no tag surface in Kodi's nodes). The writers re-inject it on every
    rewrite (W1.8), so this is the "now" half and they are the durability
    half. The Downloads node filters on exactly this tag (W1.9).
    """
    with Database("kofin") as kofin_db, Database("video") as video:
        target = _tag_target(kofin_db.cursor, download)
        if target is not None:
            KodiDownloads(video.cursor).get_tag(TAG, target[0], target[1])


def unstamp_tag(download: store.Download) -> None:
    """Drop the tag when the last reason for it goes — a movie's own removal,
    or a show's final downloaded episode. The tag row itself goes when its
    last link does (``remove_tag_when_orphaned``), so a fully removed
    download leaves zero trace."""
    with Database("kofin") as kofin_db, Database("video") as video:
        if download.media_type == "episode" and store.series_done_on(
            kofin_db.cursor, download.series_id
        ):
            return  # a sibling still holds the show in the node
        target = _tag_target(kofin_db.cursor, download)
        if target is not None:
            KodiDownloads(video.cursor).remove_tag_when_orphaned(
                TAG, target[0], target[1]
            )
