"""The apply pass: desired state against the scanner's row, patched only
where they differ, confirmed by readback, acknowledged by the batch.

Setters merge maps and replace tags, so a patch clears only the keys Kofin
owned last time and keeps what Kodi or the user added. A refresh is the one
operation that replaces a row -- cast and streams have no setter -- and a show
refresh replaces every episode with it, so their local edits are captured
first and all of them rejoin the pass.

Music is confirmed by the scope, not the row. A song's patch is its play
count and last played; thousands of them on a first import would cost a
details call each to confirm, so the kinds the scanner derives are patched
in batches and then read back in one listing per library.
"""

import collections
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

from kofin.core.log import Logger
from . import metadata
from .kinds import (
    BATCH,
    DERIVED,
    EXPECTED,
    FILED,
    KINDS,
    ORDER,
    PLAYABLE,
    PROPERTIES,
    rpc,
    rpc_batch,
)
from .readback import artist_key
from .store import Mapping, Record, payload_hash

LOG = Logger(__name__)

# A refresh takes seconds; re-listing the scope every poll would re-read a
# library dozens of times for one changed item.
REFRESH_POLL = 1.0


class Patch:
    def __init__(self, record, kodi_id, desired, compare, applied):
        self.record = record
        self.kodi_id = kodi_id
        self.desired = desired
        self.compare = compare
        self.applied = applied


class Applier:
    def __init__(self, native):
        self.native = native
        self.store = native.store
        self.key = native.key
        self.readback = native.readback
        # Per-pass write batches: thousands of one-row commits cost more than
        # the pass itself (measured: 3,685 database opens for a 1,788-movie
        # pass that sent no patch at all).
        self._acks: List[Tuple[str, int, Optional[int], Dict[str, Any], str]] = []
        self._expectations: List[Tuple[str, int, Dict[str, Any]]] = []
        self._mappings: Dict[str, Any] = {}
        self._mappings_loaded = False
        self.collections: Dict[str, str] = {}
        self.movies: Set[str] = set()
        self.local: Set[str] = set()
        # Music, per library: the album directories with a live song, the
        # artists a live album credits, and the Kodi album id each album's
        # songs were filed under -- read once a pass, not once a row.
        self._song_folders: Dict[str, Set[str]] = {}
        self._albums: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._credited: Dict[str, Set[str]] = {}
        self._album_ids: Dict[str, Dict[str, Optional[int]]] = {}
        # Kodi's album and artist rows are not per library: two libraries
        # (or two albums of one) may share a row, and one of ours owns it.
        self._album_owners: Optional[Dict[int, str]] = None
        self._artist_owners: Optional[Dict[str, str]] = None
        self._deferred: List[Patch] = []

    # -- batches -------------------------------------------------------------

    def ack(self, item_id, generation, kodi_id, applied, kind):
        self._acks.append((item_id, generation, kodi_id, applied, kind))
        if len(self._acks) >= 200:
            self.commit()

    def commit(self):
        if self._expectations:
            self.store.expect_many(self._expectations)
            self._expectations = []
        if self._acks:
            acks, self._acks = self._acks, []
            self.store.remember_many(acks)
            if self._mappings_loaded:
                # Keep the preloaded mappings current without re-reading
                # them: a 24,000-row reload every 200 acknowledgements was
                # the pass's own largest cost.
                for item_id, _, kodi_id, applied, kind in acks:
                    self._mappings[item_id] = Mapping(kodi_id, applied or {}, kind)

    def mapping(self, item_id):
        if self._mappings_loaded:
            return self._mappings.get(item_id)
        return self.store.mapping(item_id)

    # -- the pass ------------------------------------------------------------

    def run(self, upserts: Dict[str, Record], collections, movies, repair, errors):
        self.collections = collections
        self.movies = movies
        self.local = {item_id for item_id, _ in self.store.local_pending()}
        self._mappings = self.store.mappings()
        self._mappings_loaded = True
        queue: List[Patch] = []
        for kind in ORDER:
            for item_id in sorted(i for i, r in upserts.items() if r.kind == kind):
                record = upserts[item_id]
                try:
                    if self.native.abort():
                        raise InterruptedError("native sync stopped")
                    patch = self.plan(record, repair, upserts)
                    if patch is not None:
                        queue.append(patch)
                        if len(queue) >= BATCH:
                            self.flush(queue, errors)
                except InterruptedError:
                    raise
                except Exception as error:
                    self.store.failed(
                        record.item_id,
                        record.generation,
                        type(error).__name__ + ": " + str(error),
                    )
                    errors.append(error)
            self.flush(queue, errors)
            if KINDS[kind].removal == "rescan":
                self.confirm_by_scope(kind, errors)
            self.commit()

    # -- music lookups -------------------------------------------------------

    def song_folders(self, library) -> Set[str]:
        if library not in self._song_folders:
            self._song_folders[library] = set(self.store.folders("Audio", library))
        return self._song_folders[library]

    def albums(self, library) -> Dict[str, Dict[str, Any]]:
        """The library's album payloads, read once a pass: a song's tag hash
        takes its album's MusicBrainz ids, and 22,000 songs must not read
        their album 22,000 times."""
        if library not in self._albums:
            self._albums[library] = {
                i: r.item
                for i, r in self.store.records(
                    kind="MusicAlbum", library=library
                ).items()
            }
        return self._albums[library]

    def credited(self, library) -> Set[str]:
        """The artist ids the library's live albums credit."""
        if library not in self._credited:
            artists: Set[str] = set()
            for item in self.albums(library).values():
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
            for folder, rows in sorted(self.readback.folders(library).items()):
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

    def row_for(self, record: Record):
        kind = record.kind
        if kind == "Audio":
            return self.readback.scope("Audio", record.library, record.parent_id).get(
                record.item_id
            )
        if kind == "MusicAlbum":
            albumid = self.album_id(record)
            if albumid is None:
                return None
            return self.readback.scope("MusicAlbum", record.library).get(albumid)
        if kind == "MusicArtist":
            return self.readback.scope("MusicArtist", record.library).get(
                artist_key(record.item.get("Name"))
            )
        if kind == "Season":
            number = record.item.get("IndexNumber")
            if number is None:
                return None
            return self.readback.scope("Season", record.library, record.parent_id).get(
                int(number)
            )
        if kind == "BoxSet":
            name = (record.item.get("Name") or "").strip(metadata.ASCII_SPACE)
            return self.readback.scope("BoxSet").get(name)
        return self.readback.scope(kind, record.library, record.parent_id).get(
            record.item_id
        )

    def rowless(self, record: Record) -> bool:
        """Whether an item of this kind is applied with no native row.

        Kodi cannot file an unnumbered special; GetSeasons joins episodes,
        so an empty season is invisible; a set exists only once a movie Kodi
        holds has filed it under that very name -- a collection whose movies
        are outside the selected libraries, or the one a movie in two
        collections did not take, never has a row to find.
        """
        kind = record.kind
        if kind == "Episode":
            return metadata.episode_numbers(record.item) is None
        if kind == "Season":
            return not self.season_has_episodes(record)
        if kind == "MusicAlbum":
            # An album Kodi derives exists only through songs of ours in it.
            return record.item_id not in self.song_folders(record.library)
        if kind == "MusicArtist":
            # An artist no live album credits -- a guest on a track, or one
            # whose albums are outside the selection -- has no row of ours to
            # find by name; one whose albums have rows but whose name Kodi
            # spells otherwise has none either.
            if record.item_id not in self.credited(record.library):
                return True
            return self.row_for(record) is None
        if kind == "BoxSet":
            name = (record.item.get("Name") or "").strip(metadata.ASCII_SPACE)
            return not any(
                member in self.movies and self.collections.get(member) == name
                for member in record.item.get("KofinMembers") or []
            )
        return False

    def plan(self, record: Record, repair, upserts) -> Optional[Patch]:
        kind = record.kind
        if self.rowless(record):
            self.ack(record.item_id, record.generation, None, {}, kind)
            return None
        row = self.row_for(record)
        if row is None:
            if kind in ("Season", "BoxSet", "MusicAlbum"):
                # Its episodes, movies or songs are being imported this pass
                # or the next; nothing native to confirm yet.
                raise RuntimeError("%s has no native row yet" % kind)
            raise RuntimeError("scanner did not import %s" % kind)
        if kind in DERIVED or kind == "Audio":
            return self.plan_music(record, row, repair)
        seasons = (
            [
                r.item
                for r in self.store.records(
                    kind="Season", parent_id=record.item_id
                ).values()
            ]
            if kind == "Series"
            else ()
        )
        set_name = self.collections.get(record.item_id, "") if kind == "Movie" else None
        desired = metadata.details(
            record.item,
            self.native.server_url(),
            self.key,
            record.library,
            self.native.separator,
            seasons,
            set_name,
        )
        mapping = self.mapping(record.item_id)
        previous = mapping.applied if mapping else {}
        if record.item_id in self.local:
            # Deliver the real user edit first. Never overwrite it with an
            # older server snapshot during a retry or a refresh.
            return None
        if kind in PLAYABLE and previous.get("userdata"):
            edits = local_edits(row, previous["userdata"], desired)
            if edits:
                self.store.local(record.item_id, edits)
                return None
        kodi_id = row[KINDS[kind].id_param]
        if kind in FILED:
            token = desired["uniqueid"]["kofinrefresh"]
            if (row.get("uniqueid") or {}).get("kofinrefresh") != token:
                row = self.refresh(record, kodi_id, token, upserts)
                kodi_id = row[KINDS[kind].id_param]
        applied = {
            "hash": payload_hash(record.item),
            "owned": metadata.owned(desired),
        }
        compare = merge(kind, desired, row, previous.get("owned", {}))
        if kind in PLAYABLE:
            applied["userdata"] = metadata.userdata(record.item)
        if kind in EXPECTED:
            self._expectations.append(
                (record.item_id, record.generation, applied["userdata"])
            )
        # A withdrawn season name is a clear the readback cannot show.
        clearing = kind == "Season" and desired.get("title") == ""
        if (
            not clearing
            and matches(row, compare)
            and (not repair or previous.get("hash") == applied["hash"])
        ):
            self.ack(record.item_id, record.generation, kodi_id, applied, kind)
            return None
        return Patch(record, kodi_id, desired, compare, applied)

    def plan_music(self, record: Record, row, repair) -> Optional[Patch]:
        kind = record.kind
        table = KINDS[kind]
        kodi_id = row[table.id_param]
        mapping = self.mapping(record.item_id)
        previous = mapping.applied if mapping else {}
        desired = metadata.details(record.item, self.native.server_url(), self.key, "")
        applied: Dict[str, Any] = {
            "hash": payload_hash(record.item),
            "owned": metadata.owned(desired),
        }
        if kind == "Audio":
            if record.item_id in self.local:
                return None
            if previous.get("userdata"):
                edits = local_edits(row, previous["userdata"], desired)
                if edits:
                    self.store.local(record.item_id, edits)
                    return None
            album = self.albums(record.library).get(record.parent_id)
            applied["tag"] = metadata.tag_hash(record.item, album)
            applied["dir"] = record.parent_id
            applied["userdata"] = metadata.userdata(record.item)
            if previous.get("tag") not in (None, applied["tag"]) and not (
                (record.library, record.parent_id) in self.native.rescanned
                or (record.library, "*") in self.native.rescanned
            ):
                # The tags moved and the directory was not re-listed this
                # pass: the row still carries the old ones.
                raise RuntimeError("song awaits its directory's rescan")
        elif kind == "MusicAlbum":
            owner = self.album_owner(kodi_id)
            if owner is not None and owner != record.item_id:
                # Kodi merged two of our albums into one row; the first owns
                # its art and description, the other is mapped and left.
                self.ack(record.item_id, record.generation, kodi_id, {}, kind)
                return None
        elif kind == "MusicArtist":
            if self.artist_owner(record) != record.item_id:
                self.ack(record.item_id, record.generation, kodi_id, {}, kind)
                return None
        compare = merge(kind, desired, row, previous.get("owned", {}))
        if matches(row, compare) and (
            not repair or previous.get("hash") == applied["hash"]
        ):
            self.ack(record.item_id, record.generation, kodi_id, applied, kind)
            return None
        return Patch(record, kodi_id, desired, compare, applied)

    def season_has_episodes(self, season: Record) -> bool:
        number = season.item.get("IndexNumber")
        if number is None:
            return False
        for episode in self.store.records(
            kind="Episode", parent_id=season.parent_id
        ).values():
            numbers = metadata.episode_numbers(episode.item)
            if numbers is not None and numbers["season"] == int(number):
                return True
        return False

    # -- refresh -------------------------------------------------------------

    def refresh(self, record: Record, kodi_id, token, upserts):
        """Re-import a row whose cast or streams changed; confirmed by token.

        A show refresh re-creates every episode of the show, so their rows,
        identities and userdata all need another pass; they are put back to
        pending and joined to this one.
        """
        kind = record.kind
        table = KINDS[kind]
        if table.refresh is None:
            raise RuntimeError("%s has no refresh" % kind)
        self.readback.owned(
            kind, kodi_id, record.item_id, record.library, record.parent_id
        )
        params: Dict[str, Any] = {table.id_param: kodi_id, "ignorenfo": False}
        if kind == "Series":
            # The refresh deletes every episode and re-creates it from the
            # supplied tag, so a watched mark or position the viewer set in
            # Kodi since the last sync is captured first, as a local edit.
            self.capture_local_edits(record)
            params["refreshepisodes"] = True
        rpc(table.refresh, params)
        self.native._async_pending = True
        last = [0.0]

        def refreshed():
            now = time.monotonic()
            if now - last[0] < REFRESH_POLL:
                return None
            last[0] = now
            self.readback.forget(kind, record.library, record.parent_id)
            found = self.readback.scope(kind, record.library, record.parent_id).get(
                record.item_id
            )
            if found and (found.get("uniqueid") or {}).get("kofinrefresh") == token:
                return found
            return None

        row = self.native.wait(refreshed, busy=self.native._scanning)
        self.native._async_pending = False
        if kind == "Series":
            children = self.store.records(
                kind=("Season", "Episode"), parent_id=record.item_id
            )
            self.store.invalidate(children)
            for child in children.values():
                upserts.setdefault(child.item_id, child)
            self.readback.forget("Series", record.library)
        return row

    def capture_local_edits(self, show: Record):
        rows = self.readback.scope("Episode", show.library, show.item_id)
        for item_id, row in rows.items():
            mapping = self.store.mapping(item_id)
            item = self.store.item(item_id)
            if not mapping or not mapping.applied.get("userdata") or item is None:
                continue
            edits = local_edits(
                row, mapping.applied["userdata"], metadata.userdata(item)
            )
            if edits:
                self.store.local(item_id, edits)

    # -- patches -------------------------------------------------------------

    def confirm_by_scope(self, kind, errors):
        """Read the kind's rows back in one listing per library and settle
        every deferred patch against it."""
        if not self._deferred:
            return
        patches, self._deferred = self._deferred, []
        for library in sorted({p.record.library for p in patches}):
            self.readback.forget(kind, library)
        for patch in patches:
            record = patch.record
            try:
                row = self.row_for(record)
                if row is None:
                    raise RuntimeError("%s row vanished after its patch" % kind)
                if not matches(row, patch.compare):
                    fields = [
                        key
                        for key, value in patch.compare.items()
                        if not matches(row, {key: value})
                    ]
                    raise RuntimeError(
                        "%s detail readback differs: %s" % (kind, ", ".join(fields))
                    )
                self.ack(
                    record.item_id,
                    record.generation,
                    patch.kodi_id,
                    patch.applied,
                    kind,
                )
            except InterruptedError:
                raise
            except Exception as error:
                self.store.failed(
                    record.item_id,
                    record.generation,
                    type(error).__name__ + ": " + str(error),
                )
                errors.append(error)

    def flush(self, queue: List[Patch], errors):
        if not queue:
            return
        patches, queue[:] = list(queue), []
        # Expectations go in ahead of the setters they cover.
        if self._expectations:
            self.store.expect_many(self._expectations)
            self._expectations = []
        # Kodi logs no announcements, so this line is the only count of
        # what a pass actually wrote (an import whose rows match sends none).
        LOG.info("patching %d %s rows", len(patches), patches[0].record.kind.lower())
        setters: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        for patch in patches:
            table = KINDS[patch.record.kind]
            params = dict(patch.desired)
            params[table.id_param] = patch.kodi_id
            setters.append((table.setter, params))
        replies = rpc_batch(setters)
        confirmations: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        confirming = []
        for patch, reply in zip(patches, replies):
            if isinstance(reply, Exception):
                self.store.failed(
                    patch.record.item_id, patch.record.generation, str(reply)
                )
                errors.append(reply)
                continue
            table = KINDS[patch.record.kind]
            if table.removal == "rescan":
                # Confirmed by the scope once the kind's patches are all sent.
                self._deferred.append(patch)
                continue
            confirmations.append(
                (
                    table.getter,
                    {
                        table.id_param: patch.kodi_id,
                        "properties": PROPERTIES[patch.record.kind],
                    },
                )
            )
            confirming.append(patch)
        for patch, reply in zip(confirming, rpc_batch(confirmations)):
            record = patch.record
            try:
                if isinstance(reply, Exception):
                    raise reply
                row = (
                    reply.get(KINDS[record.kind].result_key, {})
                    if isinstance(reply, dict)
                    else {}
                )
                if record.kind in FILED:
                    if (
                        self.readback.owns(
                            record.kind, row, record.library, record.parent_id
                        )
                        != record.item_id
                    ):
                        raise RuntimeError("%s ownership changed" % record.kind)
                if not matches(row, patch.compare):
                    fields = [
                        key
                        for key, value in patch.compare.items()
                        if not matches(row, {key: value})
                    ]
                    raise RuntimeError(
                        "%s detail readback differs: %s"
                        % (record.kind, ", ".join(fields))
                    )
                self.ack(
                    record.item_id,
                    record.generation,
                    patch.kodi_id,
                    patch.applied,
                    record.kind,
                )
                cache_key: Any = record.item_id
                if record.kind == "Season":
                    cache_key = int(record.item.get("IndexNumber") or 0)
                elif record.kind == "BoxSet":
                    cache_key = row.get("title", "")
                self.readback.remember(
                    record.kind, record.library, record.parent_id, cache_key, row
                )
            except InterruptedError:
                raise
            except Exception as error:
                self.store.failed(
                    record.item_id,
                    record.generation,
                    type(error).__name__ + ": " + str(error),
                )
                errors.append(error)


def local_edits(row, previous, desired):
    """Userdata the viewer changed in Kodi since the last write, if any."""
    edits = {}
    playcount = row.get("playcount", 0) or 0
    if playcount != previous.get("playcount") and playcount != desired["playcount"]:
        edits["playcount"] = playcount
    if "resume" not in desired:
        # A song: Kodi's music library keeps no resume point.
        return edits
    position = (row.get("resume") or {}).get("position", 0) or 0
    if (
        abs(position - previous.get("resume", {}).get("position", 0)) >= 1
        and abs(position - desired["resume"]["position"]) >= 1
    ):
        edits["position"] = position
    return edits


def merge(kind, desired, row, owned_before):
    """Setters merge maps and replace tags. Clear only keys previously owned
    by Kofin, retaining Kodi defaults and local additions. Returns the state
    the readback must match; ``desired`` is updated in place to send."""
    compare = dict(desired)
    if "tag" in compare:
        old_tags = set(owned_before.get("tag", [])) | {
            tag for tag in row.get("tag", []) if tag.startswith("kofin.library.")
        }
        compare["tag"] = sorted(
            set(compare["tag"]) | (set(row.get("tag", [])) - old_tags)
        )
        desired["tag"] = compare["tag"]
    for field in ("art", "ratings", "uniqueid"):
        if field not in compare:
            continue
        owned = set(owned_before.get(field, []))
        merged = dict(compare[field])
        merged.update({key: None for key in owned if key not in merged})
        merged.update(
            {
                key: value
                for key, value in (row.get(field) or {}).items()
                if key not in merged and key not in owned and key != "icon"
            }
        )
        if field == "art":
            merged = {
                key: (
                    unquote(value[8:].rstrip("/"))
                    if isinstance(value, str) and value.startswith("image://")
                    else value
                )
                for key, value in merged.items()
            }
        desired[field] = merged
        compare[field] = merged
    if kind == "Season":
        if not desired.get("title"):
            if "title" in owned_before:
                desired["title"] = ""
            else:
                desired.pop("title", None)
            compare.pop("title", None)
        compare.pop("uniqueid", None)
        desired.pop("uniqueid", None)
    return compare


def matches(row, desired):
    for key, value in desired.items():
        actual = row.get(key)
        if key == "resume":
            if abs((actual or {}).get("position", 0) - value["position"]) >= 1:
                return False
        elif key in ("ratings", "uniqueid", "art"):
            actual = actual or {}
            for name, wanted in value.items():
                got = actual.get(name)
                if key == "art" and isinstance(got, str) and got.startswith("image://"):
                    got = unquote(got[8:].rstrip("/"))
                if key == "ratings" and wanted:
                    if (
                        not got
                        or abs(got["rating"] - wanted["rating"]) >= 0.01
                        or got.get("votes", 0) != wanted["votes"]
                    ):
                        return False
                elif (got or None) != (wanted or None):
                    return False
        elif key == "rating":
            if abs(float(actual or 0) - float(value or 0)) >= 0.01:
                return False
        elif isinstance(value, list):
            if sorted(actual or []) != sorted(value):
                return False
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if (actual or 0) != value:
                return False
        elif (actual or "") != (value or ""):
            return False
    return True
