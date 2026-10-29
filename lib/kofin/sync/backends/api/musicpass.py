"""The music half of a pass: the album directories Kodi must list again,
the scan that lists them, and the songs a scan lost.

Music is scanned by directory. A song is missing, or its tags have moved,
or it has moved album: each means its album directory is listed again. The
directories a pass wants listed are planned in order and scanned a part at
a time (``MusicPass.scan``), whether they are every album of a first import
or the one album a song was renamed in. ``walk`` is the order they are
listed in; this module decides which they are, runs the scan and owns the
bar across its parts. ``native`` keeps the scanner itself: the waits, the
hold and ``Native.scan``.
"""

import collections
import time
from typing import Dict, List, Optional, Set, Tuple

from kofin.core.log import Logger
from . import metadata, paths, progress, walk
from .readback import artist_key
from .records import Record
from .store import Entry, Mapping

LOG = Logger(__name__)


class MusicPass:
    """One pass's music work, and what it reads once for it: a library's
    album payloads, its song mappings and the tag hash of every song
    compared so far. The directory picker and the applier both need them,
    and a song's hash is a payload walk, a tag build and a digest.
    ``Native.reconcile`` makes one for each pass."""

    def __init__(self, native):
        self.native = native
        self.store = native.store
        self.key = native.key
        # (library, folder) pairs whose directory this pass has re-listed,
        # and (library, "*") when that was every directory of the library.
        self.rescanned: Set[Tuple[str, str]] = set()
        # Whether this pass read a library's every song (check_due).
        self.read_whole = False
        self._album_items: Dict[str, Dict[str, Dict]] = {}
        self._song_mappings: Dict[str, Dict[str, Mapping]] = {}
        self._tag_hashes: Dict[str, str] = {}
        # Libraries whose every song this pass compared with Kodi's rows.
        self._compared: Set[str] = set()
        # For the applier, per library: the album directories with a live
        # song, the artists a live album credits, and the Kodi album id each
        # album's songs were filed under -- read once a pass, not once a row.
        self._song_folders: Dict[str, Set[str]] = {}
        self._credited: Dict[str, Set[str]] = {}
        self._album_ids: Dict[str, Dict[str, Optional[int]]] = {}
        # Kodi's album and artist rows are not per library: two libraries
        # (or two albums of one) may share a row, and one of ours owns it.
        self._album_owners: Optional[Dict[int, str]] = None
        self._artist_owners: Optional[Dict[str, str]] = None

    def forget(self):
        """Drop what the pass read; what it re-listed stays on record."""
        self._album_items = {}
        self._song_mappings = {}
        self._tag_hashes = {}
        self._song_folders = {}
        self._credited = {}
        self._album_ids = {}
        self._album_owners = None
        self._artist_owners = None

    def song_mappings(self, library) -> Dict[str, Mapping]:
        """The library's song mappings, read once a pass: nothing between
        the directory picker and the lost-song check acknowledges a song."""
        if library not in self._song_mappings:
            self._song_mappings[library] = self.store.mappings(
                kind="Audio", library=library
            )
        return self._song_mappings[library]

    def album_items(self, library) -> Dict[str, Dict]:
        """The library's album payloads, read once a pass: a song's tag hash
        takes its album's artists and MusicBrainz ids."""
        if library not in self._album_items:
            self._album_items[library] = {
                i: r.item
                for i, r in self.store.records(
                    kind="MusicAlbum", library=library
                ).items()
            }
        return self._album_items[library]

    def song_folders(self, library) -> Set[str]:
        if library not in self._song_folders:
            self._song_folders[library] = set(self.store.folders("Audio", library))
        return self._song_folders[library]

    def credited(self, library) -> Set[str]:
        """The artist ids the library's live albums credit."""
        if library not in self._credited:
            artists: Set[str] = set()
            for item in self.album_items(library).values():
                for credits in (item.get("AlbumArtists"), item.get("ArtistItems")):
                    for credit in credits or []:
                        if isinstance(credit, dict) and credit.get("Id"):
                            artists.add(str(credit["Id"]))
            self._credited[library] = artists
        return self._credited[library]

    def artist_owner(self, record: Record) -> str:
        """Kodi keeps one artist per name; of our artists sharing a name the
        first by id owns the row's art and description."""
        if self._artist_owners is None:
            owners: Dict[str, str] = {}
            for item_id, found in sorted(
                self.store.records(kind="MusicArtist").items()
            ):
                owners.setdefault(artist_key(found.item.get("Name")), item_id)
            self._artist_owners = owners
        return self._artist_owners.get(
            artist_key(record.item.get("Name")), record.item_id
        )

    def album_ids(self, library) -> Dict[str, Optional[int]]:
        if library not in self._album_ids:
            ids: Dict[str, Optional[int]] = {}
            for folder, rows in sorted(self.native.readback.folders(library).items()):
                counted = collections.Counter(
                    row.get("albumid") for row in rows.values() if row.get("albumid")
                )
                if counted:
                    ids[folder] = counted.most_common(1)[0][0]
            self._album_ids[library] = ids
        return self._album_ids[library]

    def album_id(self, record: Record) -> Optional[int]:
        """Kodi's id for the album the record's songs were filed under: the
        one most of them share."""
        return self.album_ids(record.library).get(record.item_id)

    def album_owner(self, albumid) -> Optional[str]:
        """Of our albums Kodi merged into one row, the first by id owns the
        row's art and description; the others are mapped and left."""
        if self._album_owners is None:
            owners: Dict[int, str] = {}
            for library in self.store.libraries("Audio"):
                for folder, found in sorted(self.album_ids(library).items()):
                    if found is not None and (
                        found not in owners or folder < owners[found]
                    ):
                        owners[found] = folder
            self._album_owners = owners
        return self._album_owners.get(albumid)

    def tag_hash(self, record: Record) -> str:
        """A song's tag hash, computed once a pass."""
        found = self._tag_hashes.get(record.item_id)
        if found is None:
            found = self._tag_hashes[record.item_id] = metadata.tag_hash(
                record.item, self.album_items(record.library).get(record.parent_id)
            )
        return found

    def import_missing(
        self, upserts: Dict[str, Record], library, entries: Dict[str, Entry]
    ):
        """Have Kodi list every directory of the library that needs it, and
        put back to pending whatever the scan should have filed and did
        not."""
        folders = self.directories(upserts, library, entries)
        expected = sum(
            1
            for r in upserts.values()
            if r.kind == "Audio" and r.library == library and r.parent_id in folders
        )
        self.scan(library, folders, expected=expected)
        # Every directory of the library this pass re-listed, the removal's
        # scans before this one included: none other can have lost a song.
        rescanned = {folder for lib, folder in self.rescanned if lib == library}
        if rescanned:
            lost = self.lost_songs(library, rescanned, entries)
            if lost:
                self.store.invalidate(lost)
                LOG.warning(
                    "music: %d acknowledged songs missing after the scan;"
                    " pending again",
                    len(lost),
                )
        # Only now: a scan that raised leaves the library to be compared
        # whole again.
        if library in self._compared:
            self.native.music_checked.add(library)

    def scan(self, library, folders: Set[str], expected=0):
        """Have Kodi list the changed album directories again.

        The directories are planned in listing order (``walk.rows``) and
        scanned a part at a time: each scan is of a directory that lists
        ``paths.MUSIC_PART`` of them, and the scanner goes into each.
        ``expected`` is the number of songs the scan should add, for the bar.
        """
        if not folders:
            return
        every = walk.root_folders(self.store, library)
        rows = walk.rows(self.store, library, folders, self.album_items(library))
        # The applier reads the album payloads again after the scan; held
        # across it they are memory Kodi's scanner wants for its page cache.
        self._album_items.pop(library, None)
        self.store.plan_walk(library, rows)
        parts = walk.parts(len(rows))
        LOG.debug(
            "music: %d of %d directories changed; scanning them in %d part(s)",
            len(rows),
            len(every | set(folders)),
            parts,
        )
        began = time.monotonic()
        directories = [paths.music_part(self.key, library, n) for n in range(parts)]
        # One phase of the bar for the whole plan: Kodi's count of the
        # library's songs climbing towards ``expected`` across the parts,
        # where a phase a part would start the bar again thirty-two
        # directories on.
        native = self.native
        location = paths.parse(directories[0])
        heading = native.scan_heading(location, "music")
        saved = native.progress.phase(int(expected), heading) if expected else None
        try:
            native.scan(
                directories,
                "music",
                on_poll=(
                    native.scan_watch(location, heading, int(expected))
                    if saved is not None
                    else None
                ),
            )
        finally:
            if saved is not None:
                native.progress.restore(saved)
        LOG.debug(
            "music: %d directories scanned in %.1f s",
            len(rows),
            time.monotonic() - began,
        )
        self.rescanned.update((library, folder) for folder in folders)
        if every <= set(folders):
            self.rescanned.add((library, "*"))

    def lost_songs(self, library, folders: Set[str], entries) -> List[str]:
        """Acknowledged songs the scan just run should have re-filed and did
        not: a listing that failed half way, or a scan stopped between its
        delete and its re-add. Nothing pending names them, so they are found
        by comparing what was acknowledged with what Kodi holds, and go back
        to pending for the next pass to re-list their directory (salted, if
        Kodi kept the hash of the listing that lost them)."""
        everywhere = (library, "*") in self.rescanned
        if everywhere:
            self.read_whole = True
            held = self.native.readback.folders(library)
            mappings = self.song_mappings(library)
        else:
            held = {
                folder: self.native.readback.scope("Audio", library, folder)
                for folder in folders
            }
            mappings = self.store.mappings_of(
                self.store.placed_in("Audio", library, folders)
            )
        lost: List[str] = []
        for item_id, known in mappings.items():
            if known.kodi_id is None:
                continue
            placed = entries.get(item_id)
            if placed is None or placed.kind != "Audio" or placed.library != library:
                continue
            if not everywhere and placed.parent_id not in folders:
                continue
            if item_id not in held.get(placed.parent_id, {}):
                lost.append(item_id)
        return lost

    def directories(
        self, upserts: Dict[str, Record], library, entries: Dict[str, Entry]
    ) -> Set[str]:
        """The album directories whose listing Kodi must read again.

        A song with no row is missing; one whose tag hash moved since its
        row was acknowledged needs its tags re-read, which the scanner does
        only for a directory whose listing changed; one that moved album
        leaves its old directory only when that directory is listed without
        it. And a directory holding rows no live song of ours claims -- the
        old half of a move, or a listing that failed half way -- is settled
        by its complete listing too.

        Two kinds of applied song join the comparison uninvited: one Kodi
        does not hold (as ``lost_songs``), and one whose album's payload
        moved, since its tags take the album's artists and MusicBrainz ids.
        Either goes back to pending and into this pass. That whole check
        reads every song of the library, so it runs when something can
        have changed what Kodi holds (``check_due``); otherwise only the
        pass's own songs are compared, each read with its directory.
        """
        songs = {
            r.item_id: r
            for r in upserts.values()
            if r.kind == "Audio" and r.library == library
        }
        folders: Set[str] = set()
        changed_albums = {
            r.item_id
            for r in upserts.values()
            if r.kind == "MusicAlbum" and r.library == library
        }
        whole = self.check_due(library, songs, changed_albums)
        if whole:
            self.read_whole = True
            mappings = self.song_mappings(library)
            held = self.native.readback.folders(library)
            uninvited: List[str] = []
            for item_id, known in mappings.items():
                if item_id in songs or known.kodi_id is None:
                    continue
                placed = entries.get(item_id)
                if (
                    placed is None
                    or placed.kind != "Audio"
                    or placed.library != library
                ):
                    continue
                if (
                    item_id not in held.get(placed.parent_id, {})
                    or placed.parent_id in changed_albums
                ):
                    uninvited.append(item_id)
            if uninvited:
                songs.update(
                    self.store.records(
                        kind="Audio",
                        library=library,
                        item_ids=uninvited,
                        payloads=False,
                    )
                )
        else:
            mappings = self.store.mappings_of(songs)
        revived: Dict[str, Record] = {}
        if songs:
            # The bar names the phase and the log times it.
            began = time.monotonic()
            self.native.progress.note(progress.label("Audio"))

            def moved(record):
                folders.add(record.parent_id)
                if record.item_id not in upserts:
                    revived[record.item_id] = record

            # Only a song with a row and an acknowledgement has tags to
            # compare, so only its payload is read.
            tagged: List[Record] = []
            for record in songs.values():
                item_id = record.item_id
                folder = record.parent_id
                row = self.native.readback.scope("Audio", library, folder).get(item_id)
                mapping = mappings.get(item_id)
                applied = mapping.applied if mapping else {}
                if row is None:
                    moved(record)
                elif mapping is None:
                    # Imported but never acknowledged (a userdata patch
                    # failed): the row has the tags of the listing that made
                    # it unless the payload has moved since.
                    if record.generation > 1:
                        moved(record)
                else:
                    tagged.append(record)
                previous = applied.get("dir")
                if previous and previous != folder:
                    folders.add(previous)
            # The walk is the long part (a payload parsed and a tag hash for
            # each song), so it is the part the bar counts.
            saved = self.native.progress.phase(len(tagged), progress.label("Audio"))
            try:
                for record in self.native.payloads.walk(tagged):
                    self.native.progress.step("Audio")
                    if mappings[record.item_id].applied.get("tag") != self.tag_hash(
                        record
                    ):
                        moved(record)
            finally:
                self.native.progress.restore(saved)
            if revived:
                self.store.invalidate(revived)
                upserts.update(revived)
            LOG.debug(
                "music: %d songs compared in %.1f s; %d directories to scan,"
                " %d applied songs put back",
                len(songs),
                time.monotonic() - began,
                len(folders),
                len(revived),
            )
        if not whole:
            return folders
        for folder, rows in self.native.readback.folders(library).items():
            for item_id in rows:
                placed = entries.get(item_id)
                if (
                    placed is None
                    or placed.kind != "Audio"
                    or placed.library != library
                    or placed.parent_id != folder
                ):
                    folders.add(folder)
                    break
        self._compared.add(library)
        return folders

    def check_due(self, library, songs: Dict[str, Record], changed_albums) -> bool:
        """Whether this pass compares every song of the library with Kodi's
        rows: once a library after the service starts, after a repair or a
        music scan or clean of Kodi's (``Native.music_checked`` emptied),
        when an album's payload moved, and when the pass's songs span enough
        directories that the library is read whole anyway."""
        return (
            library not in self.native.music_checked
            or bool(changed_albums)
            or len({r.parent_id for r in songs.values()})
            >= self.native.readback.WHOLE_LIBRARY_FOLDERS
        )
