"""Native music through the fake Kodi: import, userdata, rescans, removals."""

import pytest

from kofin.sync.backends.api import metadata, paths
from kofin.sync.backends.api.capabilities import ALL
from kofin.sync.backends.api.readback import artist_key
from tests.unit.apikodi import song_id_of
from tests.unit.apifixtures import (  # noqa: F401
    ALBUM,
    ALBUM2,
    ARTIST,
    ARTIST2,
    LIB,
    LIB2,
    album,
    album_bundle,
    artist,
    backend,
    kodi,
    methods,
    movie,
    song,
    store,
)


def owned_songs(kodi):
    return {song_id_of(r["file"]): r for r in kodi.songs.values()}


def test_music_import_files_songs_under_album_directories(store, backend, kodi):
    items = album_bundle(songs=3) + album_bundle(ALBUM2, ARTIST2, songs=2, prefix="u")
    items[2]["UserData"] = {
        "Played": True,
        "PlayCount": 4,
        "LastPlayedDate": "2026-07-10T19:17:42Z",
    }
    store.publish(items, library=LIB)
    backend.reconcile()
    assert not store.pending()
    # Every directory is new and one part lists them both; no binding is
    # made, the music scanner needs none.
    assert kodi.music_scanned == [paths.music_part(store.namespace, LIB, 0)]
    assert sorted(kodi.music_folders) == sorted([ALBUM, ALBUM2])
    assert not methods(kodi, "VideoLibrary.SetSourceContent")
    songs = owned_songs(kodi)
    assert set(songs) == {"tc31", "tc32", "tc33", "ud41", "ud42"}
    assert songs["tc31"]["file"] == paths.playback_url(
        store.namespace, "Audio", LIB, "tc31", ALBUM, "flac"
    )
    assert songs["tc31"]["musicbrainztrackid"] == "mb-track-tc31"
    # The scanner zeroes a new song's play count: one patch, for the one
    # played song, and none for the four that match already.
    patches = methods(kodi, "AudioLibrary.SetSongDetails")
    assert len(patches) == 1 and patches[0]["playcount"] == 4
    assert patches[0]["songid"] == songs["tc31"]["songid"]
    assert patches[0]["lastplayed"] == metadata.userdata(items[2])["lastplayed"]
    assert songs["tc31"]["playcount"] == 4
    # No details call confirms a song: the scope does, once.
    assert not methods(kodi, "AudioLibrary.GetSongDetails")
    # Albums and artists are found through the songs and patched for what
    # the scanner cannot derive: art and description.
    album_row = kodi.albums[store.mapping(ALBUM).kodi_id]
    assert album_row["art"]["thumb"].endswith(
        "/Items/%s/Images/Primary?tag=cover" % ALBUM
    )
    assert album_row["description"] == "Liner notes"
    artist_row = kodi.artists[store.mapping(ARTIST).kodi_id]
    assert artist_row["description"] == "About the band"
    assert artist_row["art"]["fanart"].endswith("/Images/Backdrop/0?tag=ab")
    assert len(methods(kodi, "AudioLibrary.SetAlbumDetails")) == 2
    assert len(methods(kodi, "AudioLibrary.SetArtistDetails")) == 2
    applied = store.mapping("tc31").applied
    assert applied["dir"] == ALBUM and applied["tag"] == metadata.tag_hash(
        items[2], items[1]
    )
    # A second pass sends nothing and scans nothing.
    kodi.calls.clear()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.invalidate(["tc31", ALBUM, ARTIST])
    backend.reconcile()
    assert not kodi.music_scanned
    assert not [m for m, _ in kodi.calls if m.startswith("AudioLibrary.Set")]


def test_a_first_import_under_repair_writes_only_the_played_song(store, backend, kodi):
    """The pass after a complete enumeration runs as a repair, and the first
    of them is the import itself: a row the scanner has just filed with the
    userdata the server holds is acknowledged, never written."""
    items = album_bundle(songs=3) + album_bundle(ALBUM2, ARTIST2, songs=2, prefix="u")
    items[2]["UserData"] = {
        "Played": True,
        "PlayCount": 4,
        "LastPlayedDate": "2026-07-10T19:17:42Z",
    }
    store.publish(items, library=LIB)
    backend.reconcile(repair=True)
    assert not store.pending()
    patches = methods(kodi, "AudioLibrary.SetSongDetails")
    assert len(patches) == 1 and patches[0]["playcount"] == 4
    assert owned_songs(kodi)["tc31"]["playcount"] == 4


def test_a_repair_sends_nothing_for_a_song_whose_userdata_matches(store, backend, kodi):
    """A song's desired state is its userdata and the readback shows all of
    it, so a moved payload hash is no reason to write: the setter would
    change nothing, and without song_date_added it re-stamps the song's
    dateAdded."""
    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    kodi.calls.clear()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    # A field neither the tags nor the userdata read.
    store.publish([song("tc31", ALBUM, 1, Etag="moved")])
    backend.reconcile(repair=True)
    assert not kodi.music_scanned
    assert not methods(kodi, "AudioLibrary.SetSongDetails")
    assert not store.pending()


def test_a_repair_sends_nothing_for_an_album_whose_userdata_key_moved(
    store, backend, kodi
):
    """Jellyfin answers an album's UserData.Key with the key its user data
    was saved under or with its release group's, depending on its cache. No
    desired state reads the key, so a hash that took it in would have every
    repair rewrite those albums."""
    items = album_bundle(songs=2)
    items[1]["UserData"] = {"Key": "Band-Record", "PlayCount": 0, "Played": False}
    store.publish(items, library=LIB)
    backend.reconcile()
    kodi.calls.clear()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    key = "MusicAlbum-MusicBrainzReleaseGroup-rg"
    store.publish([dict(items[1], UserData=dict(items[1]["UserData"], Key=key))])
    backend.reconcile(repair=True)
    assert not methods(kodi, "AudioLibrary.SetAlbumDetails")
    assert not store.pending()


def test_an_acknowledgement_of_the_whole_payload_still_counts(store, backend, kodi):
    """Rows acknowledged before the key left the digest carry the whole
    payload's: refused, the first repair would rewrite every album."""
    from kofin.sync.backends.api.store import _digest, payload_hash

    items = album_bundle(songs=2)
    items[1]["UserData"] = {"Key": "Band-Record", "PlayCount": 0, "Played": False}
    store.publish(items, library=LIB)
    backend.reconcile()
    record = store.records(pinned=False)[ALBUM]
    mapping = store.mapping(ALBUM)
    legacy = dict(mapping.applied, hash=_digest(record.item))
    assert store.remember(ALBUM, record.generation, mapping.kodi_id, legacy)
    kodi.calls.clear()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    backend.reconcile(repair=True)
    assert not methods(kodi, "AudioLibrary.SetAlbumDetails")
    assert store.mapping(ALBUM).applied["hash"] == payload_hash(record.item)


def test_a_first_import_reads_no_song_payload_to_pick_its_directories(
    store, backend, kodi, monkeypatch
):
    """A song with no row is missing whatever its tags say: the directories
    of a first import are picked without one payload read."""
    store.publish(album_bundle(songs=3), library=LIB)
    before_the_scan = []
    payloads, payload = store.payloads, store.payload

    def many(item_ids):
        wanted = list(item_ids)
        if not kodi.music_scanned:
            before_the_scan.extend(wanted)
        return payloads(wanted)

    def one(item_id):
        if not kodi.music_scanned:
            before_the_scan.append(item_id)
        return payload(item_id)

    monkeypatch.setattr(store, "payloads", many)
    monkeypatch.setattr(store, "payload", one)
    backend.reconcile(repair=True)
    assert kodi.music_folders == [ALBUM]
    assert not before_the_scan
    assert not store.pending()


def test_a_pass_hashes_the_tags_of_each_song_once(store, backend, kodi, monkeypatch):
    """The directory picker and the applier both compare a song's tag hash;
    it is a payload walk, a tag build and a digest, made once a pass."""
    items = album_bundle(songs=3)
    store.publish(items, library=LIB)
    backend.reconcile()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.invalidate([i["Id"] for i in items])
    hashed = []
    tag_hash = metadata.tag_hash

    def counting(item, album=None):
        hashed.append(item["Id"])
        return tag_hash(item, album)

    monkeypatch.setattr(metadata, "tag_hash", counting)
    backend.reconcile(repair=True)
    assert sorted(hashed) == ["tc31", "tc32", "tc33"]
    assert not kodi.music_scanned
    assert not store.pending()


def test_album_and_artist_art_is_written_before_any_play_count(store, backend, kodi):
    """A played song costs a setter each and a library has thousands; the
    covers and descriptions the scanner cannot derive do not wait for them."""
    items = album_bundle(songs=2)
    items[2]["UserData"] = {
        "Played": True,
        "PlayCount": 1,
        "LastPlayedDate": "2026-07-10T19:17:42Z",
    }
    store.publish(items, library=LIB)
    backend.reconcile(repair=True)
    assert [m for m, _ in kodi.calls if m.startswith("AudioLibrary.Set")] == [
        "AudioLibrary.SetAlbumDetails",
        "AudioLibrary.SetArtistDetails",
        "AudioLibrary.SetSongDetails",
    ]
    assert not store.pending()


def test_metadata_change_rescans_only_that_album_and_keeps_ids(store, backend, kodi):
    store.publish(
        album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, 2, "u"), library=LIB
    )
    backend.reconcile()
    before = owned_songs(kodi)
    kodi.songs[before["tc31"]["songid"]]["playcount"] = 7
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    kodi.calls.clear()
    store.publish([song("tc31", ALBUM, 1, Name="Renamed")])
    backend.reconcile()
    assert kodi.music_folders == [ALBUM]
    after = owned_songs(kodi)
    # Same id, Kodi's own play count kept, the tag re-read.
    assert after["tc31"]["songid"] == before["tc31"]["songid"]
    assert after["tc31"]["title"] == "Renamed"
    assert after["tc31"]["playcount"] == 7
    # Kodi's count outran the server's: a local edit to deliver, never a
    # patch over it, and the song stays pending until it is delivered.
    assert not methods(kodi, "AudioLibrary.SetSongDetails")
    assert store.local_pending() == [("tc31", {"playcount": 7})]
    assert {i.item_id for i, _, _ in store.pending()} == {"tc31"}


def test_server_userdata_patches_without_a_rescan(store, backend, kodi):
    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.publish(
        [
            song(
                "tc31",
                ALBUM,
                1,
                UserData={"Played": True, "PlayCount": 2, "LastPlayedDate": None},
            )
        ]
    )
    backend.reconcile()
    assert not kodi.music_scanned
    patches = methods(kodi, "AudioLibrary.SetSongDetails")
    assert len(patches) == 1 and patches[0]["playcount"] == 2
    assert owned_songs(kodi)["tc31"]["playcount"] == 2
    assert not store.pending()


def test_a_play_count_reads_only_its_own_rows(store, backend, kodi, monkeypatch):
    """A play's user data is one song: its publish reads that song's
    placement, the pass reads that song's mapping and its own directory
    back from Kodi, and no song of the library beyond it, once the library
    has been compared whole since the service started."""
    from kofin.sync.backends.api import musicpass
    from kofin.sync.backends.api.readback import Readback
    from kofin.sync.backends.api.store import Store

    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    wheres, mappings, mappings_of, lost, whole, directories = [], [], [], [], [], []
    entries = Store._entries
    monkeypatch.setattr(
        Store,
        "_entries",
        lambda self, cursor, where, params: wheres.append(where)
        or entries(self, cursor, where, params),
    )
    all_mappings, some_mappings = Store.mappings, Store.mappings_of
    monkeypatch.setattr(
        Store,
        "mappings",
        lambda self, **kw: mappings.append(kw) or all_mappings(self, **kw),
    )
    monkeypatch.setattr(
        Store,
        "mappings_of",
        lambda self, ids: mappings_of.append(sorted(ids)) or some_mappings(self, ids),
    )
    monkeypatch.setattr(
        musicpass.MusicPass,
        "lost_songs",
        lambda self, *args: lost.append(args) or [],
    )
    songs, directory = Readback._songs, Readback.directory
    monkeypatch.setattr(
        Readback,
        "_songs",
        lambda self, library: whole.append(library) or songs(self, library),
    )
    monkeypatch.setattr(
        Readback,
        "directory",
        lambda self, library, folder: directories.append(folder)
        or directory(self, library, folder),
    )
    store.publish([song("tc31", ALBUM, 1, UserData={"Played": True, "PlayCount": 2})])
    assert wheres == ["removed IS NULL AND item_id IN (?)"]
    backend.reconcile()
    assert owned_songs(kodi)["tc31"]["playcount"] == 2
    assert mappings == []
    assert mappings_of == [["tc31"], ["tc31"]]
    assert lost == [] and whole == []
    # Read to plan the patch and again to confirm it.
    assert directories == [ALBUM, ALBUM]


def test_kodis_music_scan_makes_the_next_pass_compare_every_song(store, backend, kodi):
    """A song Kodi dropped outside a pass (the viewer's own clean or rescan)
    is found by the next pass after Kodi announces it, whatever that pass
    came for."""
    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    del kodi.songs[owned_songs(kodi)["tc32"]["songid"]]
    store.publish([song("tc31", ALBUM, 1, UserData={"Played": True, "PlayCount": 2})])
    backend.reconcile()
    assert "tc32" not in owned_songs(kodi)
    backend.music_checked.clear()
    store.publish([song("tc31", ALBUM, 1, UserData={"Played": True, "PlayCount": 3})])
    backend.reconcile()
    assert "tc32" in owned_songs(kodi)


def test_the_check_command_empties_the_checked_libraries_before_the_pass(
    store, monkeypatch
):
    from types import SimpleNamespace

    from tests.unit.apifixtures import Server, worker

    w = worker(store, Server({LIB: []}), [LIB], monkeypatch)
    native = SimpleNamespace(music_checked={LIB})
    w.command("CheckMusic", None)
    w.apply(native)
    assert native.music_checked == set()


def test_song_removal_is_the_directory_listed_without_it(store, backend, kodi):
    store.publish(
        album_bundle(songs=3) + album_bundle(ALBUM2, ARTIST2, 1, "u"), library=LIB
    )
    backend.reconcile()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.publish([], removed=["tc33"])
    backend.reconcile()
    assert kodi.music_folders == [ALBUM]
    assert set(owned_songs(kodi)) == {"tc31", "tc32", "ud41"}
    assert not store.pending()
    assert store.state("tc33").status == "applied"
    # The album and artist stay while a song of theirs remains.
    assert len(kodi.albums) == 2 and len(kodi.artists) == 2


def test_last_song_takes_album_and_artist_with_it(store, backend, kodi):
    store.publish(
        album_bundle(songs=1) + album_bundle(ALBUM2, ARTIST2, 1, "u"), library=LIB
    )
    backend.reconcile()
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.publish([], removed=["tc31", ALBUM, ARTIST])
    backend.reconcile()
    assert kodi.music_folders == [ALBUM]
    assert set(owned_songs(kodi)) == {"ud41"}
    assert len(kodi.albums) == 1 and len(kodi.artists) == 1
    assert not store.pending()
    assert store.mapping(ALBUM) is None and store.mapping(ARTIST) is None


def test_whole_library_removal_scans_every_emptied_directory_in_one_scan(
    store, backend, kodi
):
    items = album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, 2, "u")
    other = album_bundle("a7" * 16, "b8" * 16, 1, "v")
    membership = {i["Id"]: LIB for i in items}
    membership.update({i["Id"]: LIB2 for i in other})
    store.publish(items + other, membership=membership, complete_libraries=[LIB, LIB2])
    backend.reconcile()
    assert len(owned_songs(kodi)) == 5
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    kodi.calls.clear()
    store.publish([], library=LIB)
    backend.reconcile()
    # One scan, whose part named both emptied directories: that is how Kodi
    # was told to visit them.
    assert kodi.music_scanned == [paths.music_part(store.namespace, LIB, 0)]
    assert sorted(kodi.music_folders) == sorted([ALBUM, ALBUM2])
    assert set(owned_songs(kodi)) == {"va71"}
    assert len(kodi.albums) == 1 and len(kodi.artists) == 1
    assert not store.pending()
    assert not methods(kodi, "VideoLibrary.SetSourceContent")
    assert not methods(kodi, "AudioLibrary.Clean")
    # The other library's rows were read, not re-scanned.
    assert store.mapping("va71").kodi_id in kodi.songs


def test_unconfirmed_song_removal_stays_pending(store, backend, kodi):
    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    kodi.accept_without_apply = True
    directory = paths.music_dir(store.namespace, LIB, ALBUM)
    # The scan "succeeds" but the listing Kodi reads still names the song.
    kodi.listings[directory] = kodi._listing(LIB, ALBUM)
    store.publish([], removed=["tc32"])
    with pytest.raises(RuntimeError):
        backend.reconcile()
    assert {i.item_id for i, _, _ in store.pending()} == {"tc32"}
    assert "tc32" in owned_songs(kodi)


def test_a_song_moved_between_albums_rescans_both_directories(store, backend, kodi):
    store.publish(
        album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, 1, "u"), library=LIB
    )
    backend.reconcile()
    old_row = owned_songs(kodi)["tc32"]
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.publish([song("tc32", ALBUM2, 2, ARTIST2)])
    backend.reconcile()
    # The directory it left and the one it joined, in one scan.
    assert kodi.music_scanned == [paths.music_part(store.namespace, LIB, 0)]
    assert sorted(kodi.music_folders) == sorted([ALBUM, ALBUM2])
    songs = owned_songs(kodi)
    assert songs["tc32"]["file"] == paths.playback_url(
        store.namespace, "Audio", LIB, "tc32", ALBUM2, "flac"
    )
    assert songs["tc32"]["songid"] != old_row["songid"]
    assert len([r for r in kodi.songs.values() if song_id_of(r["file"]) == "tc32"]) == 1
    assert store.mapping("tc32").applied["dir"] == ALBUM2
    assert not store.pending()


def test_a_directory_that_lost_songs_heals_on_the_next_pass(store, backend, kodi):
    store.publish(album_bundle(songs=3), library=LIB)
    backend.reconcile()
    directory = paths.music_dir(store.namespace, LIB, ALBUM)
    # A listing that failed half way: Kodi kept one row of three.
    kodi.listings[directory] = kodi._listing(LIB, ALBUM)[:1]
    kodi.music_scan(directory)
    assert len(kodi.songs) == 1
    del kodi.listings[directory]
    # Nothing pending names the two lost songs: the next pass that touches
    # the library's music finds them by comparing what it acknowledged with
    # what Kodi holds.
    assert not store.pending()
    store.publish([song("tc31", number=1, Name="Renamed")])
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    backend.reconcile()
    assert kodi.music_folders == [ALBUM]
    assert set(owned_songs(kodi)) == {"tc31", "tc32", "tc33"}
    assert not store.pending()


def test_songs_without_an_album_file_under_their_artists_singles(store, backend, kodi):
    items = [artist(), song("s1", None, 1), song("s2", None, 2)]
    store.publish(items, library=LIB)
    backend.reconcile()
    folder = paths.SINGLES + ARTIST
    assert store.entry("s1").parent_id == folder
    songs = owned_songs(kodi)
    assert songs["s1"]["file"] == paths.playback_url(
        store.namespace, "Audio", LIB, "s1", folder, "flac"
    )
    assert not store.pending()
    # The artist is credited by no album of ours: mapped by name if Kodi has
    # it, applied without a row otherwise -- never left pending.
    assert store.state(ARTIST).status == "applied"


def test_two_libraries_merge_an_album_into_one_row_that_one_of_them_owns(
    store, backend, kodi
):
    items = album_bundle(songs=1)
    # The same artist, and an album with the same MusicBrainz id: Kodi makes
    # one album row of the two directories.
    twin = album_bundle(ALBUM2, ARTIST, 1, "u", Overview="Other notes")[1:]
    twin[0]["ProviderIds"] = items[1]["ProviderIds"]
    membership = {i["Id"]: LIB for i in items}
    membership.update({i["Id"]: LIB2 for i in twin})
    store.publish(items + twin, membership=membership, complete_libraries=[LIB, LIB2])
    backend.reconcile()
    assert len(kodi.albums) == 1
    assert store.mapping(ALBUM).kodi_id == store.mapping(ALBUM2).kodi_id
    assert not store.pending()
    # One description, not a fight between two every pass.
    row = next(iter(kodi.albums.values()))
    assert row["description"] in ("Liner notes", "Other notes")
    kodi.calls.clear()
    store.invalidate([ALBUM, ALBUM2])
    backend.reconcile()
    assert not methods(kodi, "AudioLibrary.SetAlbumDetails")


def test_music_readback_pages_and_acknowledges_in_batches(
    store, backend, kodi, monkeypatch
):
    from kofin.sync import private
    from kofin.sync.backends.api import readback

    monkeypatch.setattr(readback, "PAGE", 7)
    opens = []
    original = private.Database.__enter__

    def counting(self):
        opens.append(1)
        return original(self)

    items = [artist(), album()]
    for number in range(1, 31):
        items.append(
            song(
                "t%02d" % number,
                ALBUM,
                number,
                UserData={"Played": True, "PlayCount": number},
            )
        )
    store.publish(items, library=LIB)
    monkeypatch.setattr(private.Database, "__enter__", counting)
    backend.reconcile()
    assert not store.pending()
    assert len(methods(kodi, "AudioLibrary.SetSongDetails")) == 30
    listings = [
        p
        for p in methods(kodi, "AudioLibrary.GetSongs")
        if "properties" in p and p["limits"]["end"] > p["limits"]["start"]
    ]
    # Paged seven at a time, read whole a few times a pass, never per row.
    # A call that asks for no property is a count: the first-run gate's, and
    # the bar's during a scan.
    assert all(p["limits"]["end"] - p["limits"]["start"] == 7 for p in listings)
    assert len(listings) < 30
    # Thirty songs, and the opens do not follow them: the plan of the scan
    # and its one part are a handful more whatever the library holds.
    assert len(opens) < 48


def test_a_song_marked_unplayed_keeps_the_stamp_kodi_cannot_clear(store, backend, kodi):
    """Kodi's song setter cannot clear a last-played stamp, so a song marked
    unplayed keeps it and the pass does not ask for it empty."""
    items = album_bundle(songs=1)
    items[2]["UserData"] = {
        "Played": True,
        "PlayCount": 1,
        "LastPlayedDate": "2026-07-10T19:17:42Z",
    }
    store.publish(items, library=LIB)
    backend.reconcile()
    row = next(iter(kodi.songs.values()))
    assert row["playcount"] == 1 and row["lastplayed"]
    stamp = row["lastplayed"]
    items[2]["UserData"] = {"Played": False, "PlayCount": 0}
    store.publish([items[2]])
    backend.reconcile()
    assert row["playcount"] == 0 and row["lastplayed"] == stamp
    assert not store.pending()


def test_music_patch_failure_keeps_the_song_pending(store, backend, kodi):
    items = album_bundle(songs=1)
    items[2]["UserData"] = {"Played": True, "PlayCount": 1}
    store.publish(items, library=LIB)
    kodi.fail = "AudioLibrary.SetSongDetails"
    with pytest.raises(RuntimeError):
        backend.reconcile()
    assert {i.item_id for i, _, _ in store.pending()} == {"tc31"}
    kodi.fail = ""
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    backend.reconcile()
    assert not store.pending()
    assert not kodi.music_scanned
    assert owned_songs(kodi)["tc31"]["playcount"] == 1


def test_video_and_music_libraries_share_one_pass(store, backend, kodi):
    membership = {"a": LIB}
    items = [movie()] + album_bundle(songs=1)
    membership.update({i["Id"]: LIB2 for i in items[1:]})
    store.publish(items, membership=membership, complete_libraries=[LIB, LIB2])
    backend.reconcile()
    assert not store.pending()
    assert kodi.scanned == [paths.library_dir(store.namespace, LIB, "movies")]
    assert kodi.music_scanned == [paths.music_part(store.namespace, LIB2, 0)]
    assert artist_key(" Band ") == "band"


def test_album_and_artist_art_does_not_wait_for_the_video_scans(store, backend, kodi):
    """Selected together with a video library, a music library's albums and
    artists are applied between the music scan and the video scans."""
    membership = {"a": LIB}
    items = [movie()] + album_bundle(songs=2)
    items[3]["UserData"] = {
        "Played": True,
        "PlayCount": 1,
        "LastPlayedDate": "2026-07-10T19:17:42Z",
    }
    membership.update({i["Id"]: LIB2 for i in items[1:]})
    store.publish(items, membership=membership, complete_libraries=[LIB, LIB2])
    backend.reconcile(repair=True)
    assert not store.pending()
    order = [
        m
        for m, _ in kodi.calls
        if m.startswith("AudioLibrary.Set")
        or m in ("VideoLibrary.Scan", "AudioLibrary.Scan")
    ]
    video_scan = order.index("VideoLibrary.Scan")
    assert order.index("AudioLibrary.SetAlbumDetails") < video_scan
    assert order.index("AudioLibrary.SetArtistDetails") < video_scan
    # The play counts still go last of all, after the video kinds.
    assert order.index("AudioLibrary.SetSongDetails") > video_scan
    # Each of the two is applied once.
    assert order.count("AudioLibrary.SetAlbumDetails") == 1
    assert order.count("AudioLibrary.SetArtistDetails") == 1


def test_a_pass_with_no_video_scan_keeps_one_hold_and_its_order(store, backend, kodi):
    items = album_bundle(songs=2)
    items[2]["UserData"] = {"Played": True, "PlayCount": 1}
    store.publish(items, library=LIB)
    backend.reconcile(repair=True)
    assert kodi.hold_tokens == ["music"]
    assert not store.pending()


# -- provider, serializer, identity, coordinator ------------------------------


def five_albums():
    """Five albums of one song each, created a day apart, newest first in
    the catalogue so that only the label can order them."""
    items = []
    for day in (5, 4, 3, 2, 1):
        album_id = ("%02x" % (0xA0 + day)) * 16
        artist_id = ("%02x" % (0xB0 + day)) * 16
        items += album_bundle(
            album_id,
            artist_id,
            songs=1,
            prefix="d%d" % day,
            Name="Record %d" % day,
            DateCreated="2024-01-%02dT00:00:00.00Z" % day,
            ProviderIds={},
        )
    for item in items:
        if item["Type"] == "Audio":
            item["Album"] = "Record " + item["Id"][1]
    return items, [("%02x" % (0xA0 + day)) * 16 for day in (1, 2, 3, 4, 5)]


def test_a_scan_visits_its_directories_a_part_at_a_time_in_label_order(
    store, backend, kodi, monkeypatch
):
    """Kodi's directory cache holds fifty listings, and what is still in it
    when a scan ends is what the scanner's second look at each album is
    answered from: the directories go to the scanner a part at a time, each
    part a scan of its own, in the order one listing would have had them."""
    monkeypatch.setattr(paths, "MUSIC_PART", 2)
    items, oldest_first = five_albums()
    store.publish(items, library=LIB)
    backend.reconcile(repair=True)
    assert not store.pending()
    assert kodi.music_scanned == [
        paths.music_part(store.namespace, LIB, n) for n in range(3)
    ]
    # Oldest first on the server's creation date, so that Kodi, which
    # numbers albums as it adds them, has the newest at the top.
    assert kodi.music_folders == oldest_first
    assert len(owned_songs(kodi)) == 5
    # A change in one of the five is a scan of that one directory.
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    changed = next(i for i in items if i["Id"] == "d3a31")
    store.publish([dict(changed, Name="Renamed")])
    backend.reconcile()
    assert kodi.music_scanned == [paths.music_part(store.namespace, LIB, 0)]
    assert kodi.music_folders == [oldest_first[2]]
    assert owned_songs(kodi)["d3a31"]["title"] == "Renamed"
    assert not store.pending()


def test_a_part_is_never_a_directory_below_the_music_root():
    """Kodi takes a directory it is asked to scan to exist from its cached
    listing of the parent when it has one, and it lists a music root on its
    own account, so a part below the root would be skipped as missing once
    that listing is cached. The parts have a parent of their own, which
    nothing lists but a request for it."""
    key, library = "a" * 32, "b" * 32
    root = paths.library_dir(key, library, "music")
    part = paths.music_part(key, library, 7)
    assert not part.startswith(root)
    assert part.startswith(paths.walk_dir(key, library))
    located = paths.parse(part)
    assert (located.key, located.library, located.content) == (key, library, "music")
    assert located.walk and located.part == 7 and located.folder is None
    parent = paths.parse(paths.walk_dir(key, library))
    assert parent.walk and parent.part is None
    # An album directory and the root are neither.
    assert not paths.parse(root).walk
    assert not paths.parse(paths.music_dir(key, library, "c" * 32)).walk


def test_the_provider_lists_a_part_from_the_plan_and_the_parent_names_every_part(
    store, monkeypatch
):
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider, walk

    monkeypatch.setattr(paths, "MUSIC_PART", 2)
    items, oldest_first = five_albums()
    store.publish(items, library=LIB)
    rendered = []
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, entries, n: rendered.append(list(entries)),
    )
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda h, c: None)
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda h, **k: None)
    key = store.namespace

    def listed(url):
        provider.serve(Request(url, 1, {}))
        return [(entry[0], entry[2]) for entry in rendered[-1]]

    # Nothing planned: a part lists nothing and the parent names no part.
    assert listed(paths.music_part(key, LIB, 0)) == []
    assert listed(paths.walk_dir(key, LIB)) == []
    # The root lists every directory.
    everything = [(paths.music_dir(key, LIB, f), True) for f in oldest_first]
    assert listed(paths.library_dir(key, LIB, "music")) == everything
    # A plan of three of the five: two parts, in the plan's order.
    wanted = [oldest_first[4], oldest_first[0], oldest_first[2]]
    rows = walk.rows(store, LIB, wanted)
    assert [folder for folder, _ in rows] == [
        oldest_first[0],
        oldest_first[2],
        oldest_first[4],
    ]
    store.plan_walk(LIB, rows)
    assert listed(paths.music_part(key, LIB, 0)) == [
        (paths.music_dir(key, LIB, oldest_first[0]), True),
        (paths.music_dir(key, LIB, oldest_first[2]), True),
    ]
    assert listed(paths.music_part(key, LIB, 1)) == [
        (paths.music_dir(key, LIB, oldest_first[4]), True)
    ]
    assert listed(paths.music_part(key, LIB, 2)) == []
    assert listed(paths.walk_dir(key, LIB)) == [
        (paths.music_part(key, LIB, 0), True),
        (paths.music_part(key, LIB, 1), True),
    ]
    # A later plan replaces it.
    store.plan_walk(LIB, rows[:1])
    assert listed(paths.music_part(key, LIB, 1)) == []
    assert store.walk_size(LIB) == 1


def test_music_provider_lists_folders_with_tombstones_and_never_fails(
    store, monkeypatch
):
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    items = album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, 1, "u")
    items[2]["IndexNumber"] = "x"  # a malformed payload: the row survives reduced
    store.publish(items, library=LIB)
    rendered = []
    ended = []
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, entries, n: rendered.append(list(entries)),
    )
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda h, c: None)
    monkeypatch.setattr(
        provider.xbmcplugin, "endOfDirectory", lambda h, **k: ended.append(k)
    )
    root = paths.library_dir(store.namespace, LIB, "music")
    provider.serve(Request(root, 1, {}))
    assert [e[0] for e in rendered[-1]] == [
        paths.music_dir(store.namespace, LIB, ALBUM),
        paths.music_dir(store.namespace, LIB, ALBUM2),
    ]
    provider.serve(Request(paths.music_dir(store.namespace, LIB, ALBUM), 1, {}))
    assert [e[0] for e in rendered[-1]] == [
        paths.playback_url(store.namespace, "Audio", LIB, "tc31", ALBUM, "flac"),
        paths.playback_url(store.namespace, "Audio", LIB, "tc32", ALBUM, "flac"),
    ]
    assert all(k.get("succeeded") is True for k in ended)
    # Every song of the second album is removed: its directory stays listed
    # at the root, empty, until the scan has emptied it in Kodi.
    store.publish([], removed=["ud41"])
    provider.serve(Request(root, 1, {}))
    assert [e[0] for e in rendered[-1]][-1] == paths.music_dir(
        store.namespace, LIB, ALBUM2
    )
    provider.serve(Request(paths.music_dir(store.namespace, LIB, ALBUM2), 1, {}))
    assert rendered[-1] == []
    # The store unreadable: Kodi's own rows are listed back, not nothing.
    broken = {"on": True}
    original_records = provider.Store.records

    def records(self, *a, **k):
        if broken["on"]:
            raise OSError("locked")
        return original_records(self, *a, **k)

    monkeypatch.setattr(provider.Store, "records", records)
    monkeypatch.setattr(
        provider.kodirpc,
        "call",
        lambda m, p=None: {
            "songs": [
                {
                    "file": paths.playback_url(
                        store.namespace, "Audio", LIB, "tc31", ALBUM, "flac"
                    ),
                    "title": "Kept",
                    "artist": ["Band"],
                    "album": "Record",
                }
            ],
            "limits": {"total": 1},
        },
    )
    provider.serve(Request(paths.music_dir(store.namespace, LIB, ALBUM), 1, {}))
    assert len(rendered[-1]) == 1 and ended[-1]["succeeded"] is True
    broken["on"] = False
    # The root listing of the whole plugin names the music root.
    provider.serve(Request(paths.root(store.namespace), 1, {}))
    assert root in [e[0] for e in rendered[-1]]
    resolved = []
    monkeypatch.setattr(
        provider.xbmcplugin, "setResolvedUrl", lambda h, ok, li: resolved.append(ok)
    )
    provider.serve(
        Request(
            paths.music_dir(store.namespace, LIB, ALBUM2),
            1,
            {"kodi_action": "check_exists", "id": "ud41"},
        )
    )
    provider.serve(
        Request(
            paths.music_dir(store.namespace, LIB, ALBUM),
            1,
            {"kodi_action": "check_exists", "id": "tc31"},
        )
    )
    assert resolved == [False, True]


def test_song_tags_hash_moves_for_tags_only_and_the_row_is_marked_loaded(
    monkeypatch,
):
    from unittest.mock import Mock

    item = song("t1", UserData={"Played": True, "PlayCount": 3})
    base = metadata.tag_hash(item, album())
    assert metadata.tag_hash(song("t1"), album()) == base
    assert metadata.tag_hash(song("t1", Name="Other"), album()) != base
    assert metadata.tag_hash(song("t1"), album(ProviderIds={})) != base
    tags = metadata.song_tags(item, album())
    assert tags["musicbrainzalbumid"] == "mb-album-c3" and tags["size"] == 1001
    assert tags["year"] == 1999 and tags["releasedate"] == "1999-05-01"
    assert metadata.song_tags(song("t1", ProductionYear=1))["year"] == 0
    assert (
        metadata.hash_time(base).endswith("Z") and len(metadata.hash_time(base)) == 20
    )
    assert metadata.hash_time(base) != metadata.hash_time(
        metadata.tag_hash(song("t1", Name="Other"), album())
    )
    # Played with no date: the day the server first saw the song stands in.
    assert metadata.details(item, "http://s", "k", LIB) == {
        "playcount": 3,
        "lastplayed": metadata._timestamp("2023-11-15T19:18:34.08Z"),
    }
    assert metadata.details(song("t1"), "http://s", "k", LIB) == {"playcount": 0}
    tag = Mock()
    li = Mock()
    li.getMusicInfoTag.return_value = tag
    del tag.setLoaded
    monkeypatch.setattr(metadata.xbmcgui, "ListItem", lambda *a, **k: li)
    metadata.song_listitem(item, album(), features=ALL)
    li.setInfo.assert_called_once_with("music", {"size": "1001"})
    li.setDateTime.assert_called_once_with(metadata.hash_time(base))
    # When the server first saw it, which Kodi 23 keeps (kontell/xbmc #13);
    # not a tag, so a corrected date moves no directory's hash.
    tag.setDateAdded.assert_called_once_with(
        metadata._timestamp("2023-11-15T19:18:34.08Z")
    )
    assert (
        metadata.tag_hash(dict(item, DateCreated="2020-01-01T00:00:00Z"), album())
        == base
    )
    tag.setAlbumArtist.assert_called_once_with("Band")
    tag.setMusicBrainzAlbumID.assert_called_once_with("mb-album-c3")
    assert not tag.setPlayCount.called
    # The compaction keeps the size and drops the streams.
    compacted = metadata.compact(
        song("t1", MediaSources=[{"Size": 5, "MediaStreams": [{"Type": "Audio"}]}])
    )
    assert compacted["MediaSources"] == [{"Size": 5}]
    assert "MediaStreams" not in compacted


def test_album_and_artist_identity_lookups(store, backend, kodi, monkeypatch):
    """The Jellyfin actions menu on an album or an artist row reaches the
    one the pass acknowledged against the row."""
    from kofin.service import libraryclaim
    from kofin.sync.backends.api import identity

    monkeypatch.setattr(identity, "current_store", lambda: store)
    store.publish(
        album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, songs=1, prefix="u"),
        library=LIB,
    )
    backend.reconcile()
    albumid = store.mapping(ALBUM).kodi_id
    artistid = store.mapping(ARTIST).kodi_id
    assert identity.mapped_item(albumid, "album") == ALBUM
    assert libraryclaim.mapped_jellyfin_id(albumid, "album") == ALBUM
    assert identity.mapped_item(store.mapping(ALBUM2).kodi_id, "album") == ALBUM2
    assert identity.mapped_item(artistid, "artist") == ARTIST
    assert identity.mapped_item(store.mapping(ARTIST2).kodi_id, "artist") == ARTIST2
    kodi.artists[artistid].update(artist="Someone else", label="Someone else")
    assert identity.mapped_item(artistid, "artist") is None
    store.publish([], removed=[ALBUM])
    assert identity.mapped_item(albumid, "album") is None


def test_an_album_row_stands_for_the_album_most_of_whose_songs_it_holds(
    store, backend, kodi, monkeypatch
):
    """A song tagged with another album's title is filed on that album's
    row (no MusicBrainz id to keep it home). The row still stands for the
    album most of whose songs it holds, the one whose art the pass writes
    there (Applier.album_owner), not the first album with a song on it."""
    from kofin.sync.backends.api import identity

    monkeypatch.setattr(identity, "current_store", lambda: store)
    store.publish(
        [
            artist(),
            album(ALBUM, ProviderIds={}),
            song("t1", ALBUM, 1),
            song("t2", ALBUM, 2),
            song("t3", ALBUM, 3, Album="Other"),
            album(ALBUM2, name="Other", ProviderIds={}),
            song("u1", ALBUM2, 1, Album="Other"),
        ],
        library=LIB,
    )
    backend.reconcile()
    rows = {r["title"]: r["albumid"] for r in kodi.albums.values()}
    assert {
        r["album"] for r in kodi.songs.values() if r["albumid"] == rows["Other"]
    } == {"Other"}
    assert sorted(
        song_id_of(r["file"])
        for r in kodi.songs.values()
        if r["albumid"] == rows["Other"]
    ) == ["t3", "u1"]
    assert store.mapping(ALBUM2).kodi_id == rows["Other"]
    assert identity.mapped_item(rows["Other"], "album") == ALBUM2
    assert identity.mapped_item(rows["Record"], "album") == ALBUM


def test_song_identity_lookups_use_the_url(store, backend, kodi, monkeypatch):
    from kofin.service import libraryclaim
    from kofin.sync.backends.api import identity

    monkeypatch.setattr(identity, "current_store", lambda: store)
    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    songid = store.mapping("tc31").kodi_id
    assert identity.mapped_item(songid, "song") == "tc31"
    assert identity.mapped_item(songid, "movie") is None
    assert identity.native_id_for("tc31", "song") == songid
    assert libraryclaim.mapped_jellyfin_id(songid, "song") == "tc31"
    assert identity.library_url(ALBUM) is None
    kodi.songs[songid]["file"] = "/foreign.flac"
    assert identity.mapped_item(songid, "song") is None
    # The fallback finds the row through its directory, not the library.
    kodi.songs[songid]["file"] = paths.playback_url(
        store.namespace, "Audio", LIB, "tc31", ALBUM, "flac"
    )
    kodi.calls.clear()
    assert identity.native_id_for("tc31", "song") == songid
    directory_reads = [
        p
        for p in methods(kodi, "AudioLibrary.GetSongs")
        if p.get("filter", {}).get("value", "").endswith(ALBUM + "/")
    ]
    assert not directory_reads or len(directory_reads) == 1


def test_music_enumeration_lists_three_kinds_with_light_fields(store, monkeypatch):
    from tests.unit.apifixtures import Server, worker

    items = album_bundle(songs=2)
    server = Server(
        {LIB: items},
        views=[{"Id": LIB, "CollectionType": "music", "Name": "Tunes"}],
    )
    seen = []
    original = server.items

    def spy(params):
        seen.append(params)
        return original(params)

    server.items = spy
    w = worker(store, server, [LIB], monkeypatch)
    w.full_sync()
    records = store.records(pinned=False)
    assert {r.kind for r in records.values()} == {"MusicArtist", "MusicAlbum", "Audio"}
    assert records["tc31"].parent_id == ALBUM
    music_pages = [p for p in seen if p.get("IncludeItemTypes") == "Audio"]
    assert music_pages and "RecursiveItemCount" not in music_pages[0]["Fields"]
    assert music_pages[0]["Limit"] == 500
    # A changed song is placed through its album when the feed says nothing.
    server.libraries[LIB][2]["Name"] = "Fresh"
    store.set_watermark(watermark="x", enumerated=1.0)
    w.command("changed", ["tc31"])
    assert store.records(pinned=False)["tc31"].item["Name"] == "Fresh"


def test_music_patches_run_under_a_held_scanner(store, backend, kodi):
    """Every song written is announced, and the home widgets re-query the
    library on each announcement unless the music scanner is busy; so the
    pass holds the scanner for the burst and lets go at the end."""
    items = album_bundle(songs=3)
    for item in items[2:]:
        item["UserData"] = {"Played": True, "PlayCount": 2}
    store.publish(items, library=LIB)
    backend.reconcile()
    calls = [(m, p) for m, p in kodi.calls if m.startswith("AudioLibrary.S")]
    hold = paths.hold_dir(store.namespace, "music")
    first_patch = next(i for i, (m, _) in enumerate(calls) if m.endswith("SongDetails"))
    taken = [i for i, (m, p) in enumerate(calls) if p.get("directory") == hold]
    assert taken and taken[0] < first_patch
    assert kodi.hold_tokens == ["music"]
    assert not kodi.holding
    assert not store.pending()
    # A pass with nothing to write takes no hold.
    kodi.calls.clear()
    kodi.hold_tokens.clear()
    store.invalidate(["tc31"])
    backend.reconcile()
    assert not kodi.hold_tokens


def test_a_burst_of_setters_takes_its_own_callbacks_off_kodis_queue(
    store, backend, kodi
):
    """Every setter announces its row, and Kodi queues a callback on the
    pass's own Monitor for each: the pass waits on the Monitor after every
    batch it sends, the only thing that takes them off again, and never
    with the zero timeout that Kodi reads as "until shutdown"."""
    waits = []

    def wait(seconds):
        waits.append((seconds, len(methods(kodi, "AudioLibrary.SetSongDetails"))))
        return False

    backend.monitor.waitForAbort = wait
    items = [artist(), album()]
    for number in range(1, 61):
        items.append(
            song(
                "t%02d" % number,
                ALBUM,
                number,
                UserData={"Played": True, "PlayCount": number},
            )
        )
    store.publish(items, library=LIB)
    backend.reconcile()
    assert not store.pending()
    assert all(seconds > 0 for seconds, _ in waits)
    # Once after each of the three song batches (25, 25, 10), at the least.
    assert {25, 50, 60} <= {sent for _, sent in waits}


def test_provider_hold_listing_waits_for_the_token_and_lists_nothing(
    store, monkeypatch
):
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    rendered = []
    ended = []
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, entries, n: rendered.append(list(entries)),
    )
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda h, c: None)
    monkeypatch.setattr(
        provider.xbmcplugin, "endOfDirectory", lambda h, **k: ended.append(k)
    )
    polls = {"n": 0}

    def token(scanner):
        polls["n"] += 1
        return "music:abc" if polls["n"] < 4 else ""

    monkeypatch.setattr(provider.state, "native_hold", token)
    monkeypatch.setattr(
        provider.xbmc,
        "Monitor",
        lambda: type("M", (), {"waitForAbort": lambda s, t: False})(),
    )
    provider.serve(Request(paths.hold_dir(store.namespace, "music"), 1, {}))
    assert polls["n"] == 4
    assert rendered[-1] == [] and ended[-1]["succeeded"] is True
    assert paths.parse(paths.hold_dir(store.namespace, "music")).hold == "music"


def test_a_directory_the_scanner_skips_is_salted_until_it_imports(
    store, backend, kodi, monkeypatch
):
    """A scan stopped before its songs were written leaves Kodi a directory
    hash with no rows, and every later scan skips the directory "due to no
    change". A by-name scan that leaves the songs missing bumps the
    directory's salt, which moves every listed date and the folder's label,
    so the next scan sees a change."""
    from kofin.sync.backends.api import metadata

    items = album_bundle(songs=2)
    store.publish(items, library=LIB)
    album = next(i for i in items if i["Type"] == "MusicAlbum")
    folder = album["Id"]
    song = next(i for i in items if i["Type"] == "Audio")
    skipped = kodi._scan_folder
    monkeypatch.setattr(kodi, "_scan_folder", lambda library, folder: None)
    # The album is planned first and has no row either; the salt is the
    # song's failure.
    with pytest.raises(RuntimeError, match="remain pending"):
        backend.reconcile()
    assert "directory salted (1)" in store.state(song["Id"]).error
    assert store.salt(folder) == 1
    assert metadata.folder_label(album, folder, 1).endswith(" 1")
    assert metadata.folder_label(album, folder, 0).endswith(folder)
    tags = metadata.song_tags(song, album)
    assert metadata.hash_time(metadata._token([tags, 1])) != metadata.hash_time(
        metadata._token(tags)
    )
    monkeypatch.setattr(kodi, "_scan_folder", skipped)
    backend.reconcile()
    assert not store.pending()
    assert store.salt(folder) == 1


def test_applied_songs_a_half_failed_relisting_removed_are_found_after_the_scan(
    store, backend, kodi
):
    """One song's tags move, the re-listing fails after its row, and Kodi,
    which removes every song on the path first, keeps that one. The other
    two were acknowledged long ago and nothing pending names them: the pass
    compares what it acknowledged with what the scan left and puts them
    back, and the next pass re-lists the directory."""
    store.publish(album_bundle(songs=3), library=LIB)
    backend.reconcile()
    directory = paths.music_dir(store.namespace, LIB, ALBUM)
    store.publish([song("tc31", number=1, Name="Renamed")])
    kodi.listings[directory] = kodi._listing(LIB, ALBUM)[:1]
    backend.reconcile()
    assert set(owned_songs(kodi)) == {"tc31"}
    assert len(store.pending()) == 2
    del kodi.listings[directory]
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    backend.reconcile()
    assert kodi.music_folders == [ALBUM]
    assert set(owned_songs(kodi)) == {"tc31", "tc32", "tc33"}
    assert not store.pending()


def test_an_album_change_retags_its_songs_without_a_song_payload_change(
    store, backend, kodi
):
    """A song's tags take the album's artists and MusicBrainz ids, which its
    own payload hash never sees; the album's change has to reach them."""
    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    before = store.mapping("tc31").applied["tag"]
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.publish([album(ProviderIds={"MusicBrainzAlbum": "mb-album-moved"})])
    backend.reconcile()
    assert kodi.music_folders == [ALBUM]
    assert store.mapping("tc31").applied["tag"] != before
    assert store.mapping("tc31").applied["tag"] == metadata.tag_hash(
        store.records(item_ids=["tc31"])["tc31"].item,
        store.records(item_ids=[ALBUM])[ALBUM].item,
    )
    assert not store.pending()
    # An album change that moves no tag scans nothing.
    kodi.music_scanned.clear()
    kodi.music_folders.clear()
    store.publish(
        [album(ProviderIds={"MusicBrainzAlbum": "mb-album-moved"}, Overview="x")]
    )
    backend.reconcile()
    assert kodi.music_scanned == [] and not store.pending()


def test_music_folder_waits_for_a_proven_listing(store, monkeypatch):
    """A failed or partial fallback is the directory deleted, so the listing
    waits until the store or a complete readback answers, and at shutdown
    hands Kodi nothing at all."""
    from kofin.core import kodirpc
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    store.publish(album_bundle(songs=2), library=LIB)
    rendered, ended, waits = [], [], []
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, entries, n: rendered.append(list(entries)),
    )
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda h, c: None)
    monkeypatch.setattr(
        provider.xbmcplugin, "endOfDirectory", lambda h, **k: ended.append(k)
    )
    monkeypatch.setattr(
        provider.Store,
        "records",
        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")),
    )
    kept = {
        "file": paths.playback_url(
            store.namespace, "Audio", LIB, "tc31", ALBUM, "flac"
        ),
        "title": "Kept",
    }
    replies = [
        kodirpc.FAILED,
        # A partial readback: a total of one with no row in the page.
        {"songs": [], "limits": {"total": 1}},
        {"songs": [kept], "limits": {"total": 1}},
    ]
    monkeypatch.setattr(provider.kodirpc, "call", lambda m, p=None: replies.pop(0))

    def wait(seconds):
        waits.append(seconds)
        assert len(waits) < 10, "the listing never settled"
        return False

    monkeypatch.setattr(provider, "_wait", wait)
    provider.serve(Request(paths.music_dir(store.namespace, LIB, ALBUM), 1, {}))
    assert len(waits) == 2
    assert [e[0] for e in rendered[-1]] == [kept["file"]]
    assert ended[-1]["succeeded"] is True
    # Shutdown while nothing has answered: no listing, not an empty one.
    rendered.clear()
    ended.clear()
    monkeypatch.setattr(provider.kodirpc, "call", lambda m, p=None: None)
    monkeypatch.setattr(provider, "_wait", lambda s: True)
    provider.serve(Request(paths.music_dir(store.namespace, LIB, ALBUM), 1, {}))
    assert rendered == [] and ended == []


def test_container_of_takes_the_first_name_kodi_knows():
    assert paths.container_of({"Container": "mov,mp4,m4a,3gp,3g2,mj2"}) == "mp4"
    assert paths.container_of({"MediaSources": [{"Container": "FLAC"}]}) == "flac"
    assert paths.container_of({"Container": "ra"}) == paths.FALLBACK_CONTAINER


def test_a_song_the_removals_listing_dropped_is_found_in_a_light_pass(
    store, backend, kodi
):
    """A removal re-lists the directories it shed songs from; when that is
    every directory of the library, a kept song the listing dropped is found
    by the lost-song check even though this pass planned no scan of its own
    (a play count is all it holds)."""
    bundle = album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, 2, "u")
    store.publish(bundle, library=LIB)
    backend.reconcile()
    kept = "u%s1" % ALBUM2[:2]
    gone = {"t%s2" % ALBUM[:2], "u%s2" % ALBUM2[:2]}
    listing = [item for item in bundle if item["Id"] not in gone]
    for item in listing:
        if item["Id"] == "t%s1" % ALBUM[:2]:
            item["UserData"] = {"Played": True, "PlayCount": 2}
    directory = paths.music_dir(store.namespace, LIB, ALBUM2)
    kodi.listings[directory] = []
    store.publish(listing, library=LIB)
    backend.reconcile()
    assert kept not in owned_songs(kodi)
    assert kept in {item.item_id for item, _, _ in store.pending()}
    del kodi.listings[directory]
    backend.reconcile()
    assert kept in owned_songs(kodi)


def test_a_scan_that_fails_leaves_the_library_to_be_compared_whole(
    store, backend, kodi, monkeypatch
):
    """The library counts as compared only once its scan and the lost-song
    check after it have run."""
    from kofin.sync.backends.api import musicpass

    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    backend.music_checked.clear()

    def scan(self, library, folders, expected=0):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(musicpass.MusicPass, "scan", scan)
    store.publish([song("t%s1" % ALBUM[:2], ALBUM, 1, UserData={"PlayCount": 2})])
    with pytest.raises(RuntimeError):
        backend.reconcile()
    assert LIB not in backend.music_checked
