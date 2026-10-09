"""Native music through the fake Kodi: import, userdata, rescans, removals."""

import pytest

from kofin.sync.backends.api import metadata, native, paths
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
    # Each album directory is scanned by name (few of them) and no binding is
    # made: the music scanner needs none.
    assert kodi.music_scanned == [
        paths.music_dir(store.namespace, LIB, ALBUM),
        paths.music_dir(store.namespace, LIB, ALBUM2),
    ]
    assert not methods(kodi, "VideoLibrary.SetSourceContent")
    songs = owned_songs(kodi)
    assert set(songs) == {"tc31", "tc32", "tc33", "ud41", "ud42"}
    assert songs["tc31"]["file"] == paths.playback_url(
        store.namespace, "Audio", LIB, "tc31", ALBUM
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
    store.invalidate(["tc31", ALBUM, ARTIST])
    backend.reconcile()
    assert not kodi.music_scanned
    assert not [m for m, _ in kodi.calls if m.startswith("AudioLibrary.Set")]


def test_metadata_change_rescans_only_that_album_and_keeps_ids(store, backend, kodi):
    store.publish(
        album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, 2, "u"), library=LIB
    )
    backend.reconcile()
    before = owned_songs(kodi)
    kodi.songs[before["tc31"]["songid"]]["playcount"] = 7
    kodi.music_scanned.clear()
    kodi.calls.clear()
    store.publish([song("tc31", ALBUM, 1, Name="Renamed")])
    backend.reconcile()
    assert kodi.music_scanned == [paths.music_dir(store.namespace, LIB, ALBUM)]
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


def test_song_removal_is_the_directory_listed_without_it(store, backend, kodi):
    store.publish(album_bundle(songs=3), library=LIB)
    backend.reconcile()
    kodi.music_scanned.clear()
    store.publish([], removed=["tc33"])
    backend.reconcile()
    assert kodi.music_scanned == [paths.music_dir(store.namespace, LIB, ALBUM)]
    assert set(owned_songs(kodi)) == {"tc31", "tc32"}
    assert not store.pending()
    assert store.state("tc33").status == "applied"
    # The album and artist stay while a song of theirs remains.
    assert len(kodi.albums) == 1 and len(kodi.artists) == 1


def test_last_song_takes_album_and_artist_with_it(store, backend, kodi):
    store.publish(
        album_bundle(songs=1) + album_bundle(ALBUM2, ARTIST2, 1, "u"), library=LIB
    )
    backend.reconcile()
    kodi.music_scanned.clear()
    store.publish([], removed=["tc31", ALBUM, ARTIST])
    backend.reconcile()
    assert kodi.music_scanned == [paths.music_dir(store.namespace, LIB, ALBUM)]
    assert set(owned_songs(kodi)) == {"ud41"}
    assert len(kodi.albums) == 1 and len(kodi.artists) == 1
    assert not store.pending()
    assert store.mapping(ALBUM) is None and store.mapping(ARTIST) is None


def test_whole_library_removal_walks_the_root_once_with_tombstone_folders(
    store, backend, kodi, monkeypatch
):
    monkeypatch.setattr(native, "ROOT_SCAN_ABOVE", 1)
    items = album_bundle(songs=2) + album_bundle(ALBUM2, ARTIST2, 2, "u")
    other = album_bundle("a7" * 16, "b8" * 16, 1, "v")
    membership = {i["Id"]: LIB for i in items}
    membership.update({i["Id"]: LIB2 for i in other})
    store.publish(items + other, membership=membership, complete_libraries=[LIB, LIB2])
    backend.reconcile()
    assert len(owned_songs(kodi)) == 5
    kodi.music_scanned.clear()
    kodi.calls.clear()
    store.publish([], library=LIB)
    backend.reconcile()
    root = paths.library_dir(store.namespace, LIB, "music")
    assert kodi.music_scanned == [root]
    # The root listing still named both emptied directories: that is how
    # Kodi was told to visit them.
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
    store.publish([song("tc32", ALBUM2, 2, ARTIST2)])
    backend.reconcile()
    assert set(kodi.music_scanned) == {
        paths.music_dir(store.namespace, LIB, ALBUM),
        paths.music_dir(store.namespace, LIB, ALBUM2),
    }
    songs = owned_songs(kodi)
    assert songs["tc32"]["file"] == paths.playback_url(
        store.namespace, "Audio", LIB, "tc32", ALBUM2
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
    store.invalidate(["tc31", "tc32", "tc33"])
    kodi.music_scanned.clear()
    backend.reconcile()
    assert kodi.music_scanned == [directory]
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
        store.namespace, "Audio", LIB, "s1", folder
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
    listings = [p for p in methods(kodi, "AudioLibrary.GetSongs") if "filter" in p]
    # Paged seven at a time, read whole a few times a pass, never per row.
    assert all(p["limits"]["end"] - p["limits"]["start"] == 7 for p in listings)
    assert len(listings) < 30
    assert len(opens) < 40


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
    assert kodi.music_scanned == [paths.music_dir(store.namespace, LIB2, ALBUM)]
    assert artist_key(" Band ") == "band"


# -- provider, serializer, identity, coordinator ------------------------------


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
        paths.playback_url(store.namespace, "Audio", LIB, "tc31", ALBUM),
        paths.playback_url(store.namespace, "Audio", LIB, "tc32", ALBUM),
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
                        store.namespace, "Audio", LIB, "tc31", ALBUM
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
    # No date with a count: Kodi stamps its own, as the video kinds let it.
    assert metadata.details(item, "http://s", "k", LIB) == {"playcount": 3}
    assert metadata.details(song("t1"), "http://s", "k", LIB) == {
        "playcount": 0,
        "lastplayed": "",
    }
    tag = Mock()
    li = Mock()
    li.getMusicInfoTag.return_value = tag
    del tag.setLoaded
    monkeypatch.setattr(metadata.xbmcgui, "ListItem", lambda *a, **k: li)
    metadata.song_listitem(item, album())
    li.setInfo.assert_called_once_with("music", {"size": "1001"})
    li.setDateTime.assert_called_once_with(metadata.hash_time(base))
    tag.setAlbumArtist.assert_called_once_with("Band")
    tag.setMusicBrainzAlbumID.assert_called_once_with("mb-album-c3")
    assert not tag.setPlayCount.called
    # The compaction keeps the size and drops the streams.
    compacted = metadata.compact(
        song("t1", MediaSources=[{"Size": 5, "MediaStreams": [{"Type": "Audio"}]}])
    )
    assert compacted["MediaSources"] == [{"Size": 5}]
    assert "MediaStreams" not in compacted


def test_song_identity_lookups_use_the_url(store, backend, kodi, monkeypatch):
    from kofin.service import libraryclaim
    from kofin.sync.backends.api import identity

    monkeypatch.setattr(identity, "current_store", lambda: store)
    monkeypatch.setattr(libraryclaim.buildconfig, "BACKEND", "api")
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
        store.namespace, "Audio", LIB, "tc31", ALBUM
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
