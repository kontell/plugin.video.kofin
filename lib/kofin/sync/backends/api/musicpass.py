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

import time
from typing import Dict, List, Set, Tuple

from kofin.core.log import Logger
from . import metadata, paths, progress, walk
from .records import Record
from .store import Entry

LOG = Logger(__name__)


class MusicPass:
    """One pass's music work, and what it reads once for it: a library's
    album payloads and the tag hash of every song compared so far. The
    directory picker and the applier both need them, and a song's hash is
    a payload walk, a tag build and a digest. ``Native.reconcile`` makes
    one for each pass."""

    def __init__(self, native):
        self.native = native
        self.store = native.store
        self.key = native.key
        # (library, folder) pairs whose directory this pass has re-listed,
        # and (library, "*") when that was every directory of the library.
        self.rescanned: Set[Tuple[str, str]] = set()
        self._album_items: Dict[str, Dict[str, Dict]] = {}
        self._tag_hashes: Dict[str, str] = {}

    def forget(self):
        """Drop what the pass read; what it re-listed stays on record."""
        self._album_items = {}
        self._tag_hashes = {}

    def album_items(self, library) -> Dict[str, Dict]:
        """The library's album payloads, read once a pass: a song's tag hash
        takes its album's artists and MusicBrainz ids, and 22,000 songs must
        not read their album 22,000 times."""
        if library not in self._album_items:
            self._album_items[library] = {
                i: r.item
                for i, r in self.store.records(
                    kind="MusicAlbum", library=library
                ).items()
            }
        return self._album_items[library]

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
        lost = self.lost_songs(library, folders, entries)
        if lost:
            self.store.invalidate(lost)
            LOG.warning(
                "music: %d acknowledged songs missing after the scan; pending again",
                len(lost),
            )

    def scan(self, library, folders: Set[str], expected=0):
        """Have Kodi list the changed album directories again.

        The directories are planned in listing order (``walk.rows``) and
        scanned a part at a time: each scan is of a directory that lists
        ``paths.MUSIC_PART`` of them, and the scanner goes into each. That
        is one scan job for every thirty-two directories where a scan by
        name was one for each, and it lists no directory that did not
        change, where a walk of the library root listed them all. It is
        also the only one of the three that is safe from Kodi's directory
        cache: a directory scanned by name "does not exist" to Kodi when a
        cached listing of its parent fails to name it, a new album under a
        root listed before it arrived (``paths.MUSIC_PART`` has the rest).
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
        LOG.info(
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
        LOG.info(
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
        held = self.native.readback.folders(library)
        lost: List[str] = []
        for item_id, known in self.store.mappings(
            kind="Audio", library=library
        ).items():
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

        Two kinds of applied song join the comparison uninvited. One Kodi
        no longer holds -- a listing that failed half way, a scan stopped
        between its delete and its re-add, a user's rescan interrupted:
        nothing pending names it, so it is found by comparing what was
        acknowledged with what Kodi has. And one whose album's payload moved:
        its tags take the album's artists and MusicBrainz ids, which its own
        payload hash never sees. Either goes back to pending and into this
        pass, so the scan re-files it and the applier acknowledges it again.
        """
        songs = {
            r.item_id: r
            for r in upserts.values()
            if r.kind == "Audio" and r.library == library
        }
        folders: Set[str] = set()
        mappings = self.store.mappings(kind="Audio", library=library)
        held = self.native.readback.folders(library)
        changed_albums = {
            r.item_id
            for r in upserts.values()
            if r.kind == "MusicAlbum" and r.library == library
        }
        uninvited: List[str] = []
        for item_id, known in mappings.items():
            if item_id in songs or known.kodi_id is None:
                continue
            placed = entries.get(item_id)
            if placed is None or placed.kind != "Audio" or placed.library != library:
                continue
            if (
                item_id not in held.get(placed.parent_id, {})
                or placed.parent_id in changed_albums
            ):
                uninvited.append(item_id)
        if uninvited:
            songs.update(
                self.store.records(
                    kind="Audio", library=library, item_ids=uninvited, payloads=False
                )
            )
        revived: Dict[str, Record] = {}
        if songs:
            # Six minutes on a Raspberry Pi for 22,000 songs (a payload parse
            # and a tag hash each): the bar names the phase and the log times it.
            began = time.monotonic()
            self.native.progress.note(progress.label("Audio"))

            def moved(record):
                folders.add(record.parent_id)
                if record.item_id not in upserts:
                    revived[record.item_id] = record

            # Only a song with a row and an acknowledgement has tags to
            # compare, and only its payload is read: a first import has
            # neither for any song, and walking 22,000 payloads to decide
            # that every directory is new cost a Raspberry Pi 21 s.
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
            LOG.info(
                "music: %d songs compared in %.1f s; %d directories to scan,"
                " %d applied songs put back",
                len(songs),
                time.monotonic() - began,
                len(folders),
                len(revived),
            )
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
        return folders
