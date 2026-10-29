"""Jellyfin playlists as Kodi playlist files."""

from kofin.sync import private
from kofin.sync.backends.api import paths
from kofin.sync.backends.api import playlists as api_playlists
from tests.unit.apifixtures import (  # noqa: F401
    ALBUM,
    LIB,
    LIB2,
    album_bundle,
    backend,
    kodi,
    movie,
    store,
)


class FakeApi:
    def __init__(self, lists, items):
        self.lists = lists
        self.items_by_list = items

    def playlists(self):
        return self.lists

    def playlist_items(self, playlist_id, start_index=0, limit=100):
        items = self.items_by_list.get(playlist_id, [])
        return {
            "Items": items[start_index : start_index + limit],
            "TotalRecordCount": len(items),
        }

    def item(self, playlist_id):
        return next((p for p in self.lists if p["Id"] == playlist_id), None)


def synced(store, *pairs):
    sync = private.get_sync()
    sync["Whitelist"] = [library for library, _ in pairs]
    private.save_sync(sync)
    with private.Database() as db:
        for library, media in pairs:
            db.cursor.execute(
                "INSERT OR REPLACE INTO view VALUES (?,?,?)", (library, "Lib", media)
            )


def song_item(item_id, name, track, artists=("Band",), ticks=1800000000):
    return {
        "Id": item_id,
        "Type": "Audio",
        "Name": name,
        "IndexNumber": track,
        "Artists": list(artists),
        "RunTimeTicks": ticks,
    }


def test_entries_come_from_the_catalogue_and_only_for_filed_rows(store, backend, kodi):
    backend.setup()
    store.publish([movie()], library=LIB)
    store.publish(album_bundle(songs=2), library=LIB2)
    backend.reconcile()
    resolver = api_playlists.Resolver(store)
    movie_entry = resolver.entry(
        {"Id": "a", "Type": "Movie", "Name": "Fixture a"}, "Video"
    )
    assert movie_entry.path == paths.playback_url(store.namespace, "Movie", LIB, "a")
    assert movie_entry.title == "Fixture a"
    song = resolver.entry(song_item("tc31", "Opener", 1), "Audio")
    assert song.path == paths.playback_url(
        store.namespace,
        "Audio",
        LIB2,
        "tc31",
        ALBUM,
        paths.container_of(store.item("tc31")),
    )
    assert (song.title, song.artist, song.track, song.duration) == (
        "Opener",
        "Band",
        1,
        180,
    )
    # The wrong side, an unknown item and an item Kodi has not filed yet.
    assert resolver.entry({"Id": "a", "Type": "Movie"}, "Audio") is None
    assert resolver.entry({"Id": "zz", "Type": "Movie"}, "Video") is None
    store.publish([movie("b")], library=LIB)
    assert resolver.entry({"Id": "b", "Type": "Movie"}, "Video") is None


def test_playlists_are_written_in_server_order_with_duplicates(
    store, backend, kodi, tmp_path, monkeypatch
):
    backend.setup()
    store.publish([movie()], library=LIB)
    store.publish(album_bundle(songs=2), library=LIB2)
    backend.reconcile()
    synced(store, (LIB, "movies"), (LIB2, "music"))
    monkeypatch.setattr(api_playlists, "wanted", lambda: True)
    api = FakeApi(
        [
            {"Id": "p-audio", "Name": "Gym", "MediaType": "Audio", "Etag": "e1"},
            {"Id": "p-video", "Name": "Night in", "MediaType": "Video", "Etag": "e2"},
        ],
        {
            "p-audio": [
                song_item("tc32", "Closer", 2),
                song_item("tc31", "Opener", 1),
                song_item("tc32", "Closer", 2),
                song_item("tc39", "Not synced", 9),
            ],
            "p-video": [{"Id": "a", "Type": "Movie", "Name": "Fixture a"}],
        },
    )
    music_root = str(tmp_path / "music")
    video_root = str(tmp_path / "video")
    stats = api_playlists.reconcile(
        api, store, music_root=music_root, video_root=video_root
    )
    assert (
        stats["playlists"],
        stats["written"],
        stats["tracks"],
        stats["skipped"],
    ) == (
        2,
        2,
        4,
        1,
    )
    lines = (tmp_path / "music" / "Gym.m3u8").read_text(encoding="utf-8").splitlines()
    container = paths.container_of(store.item("tc31"))
    t1 = paths.playback_url(store.namespace, "Audio", LIB2, "tc31", ALBUM, container)
    t2 = paths.playback_url(store.namespace, "Audio", LIB2, "tc32", ALBUM, container)
    assert lines == [
        "#EXTM3U",
        "#EXTINF:180,02. Band - Closer",
        t2,
        "#EXTINF:180,01. Band - Opener",
        t1,
        "#EXTINF:180,02. Band - Closer",
        t2,
    ]
    assert (tmp_path / "video" / "Night in.m3u8").read_text(encoding="utf-8") == (
        "#EXTM3U\n#EXTINF:-1,Fixture a\n%s\n"
        % paths.playback_url(store.namespace, "Movie", LIB, "a")
    )
    # Unchanged playlists are not rewritten; a dropped one is pruned.
    again = api_playlists.reconcile(
        api, store, music_root=music_root, video_root=video_root
    )
    assert again["written"] == 0
    api.lists = api.lists[:1]
    pruned = api_playlists.reconcile(
        api, store, music_root=music_root, video_root=video_root
    )
    assert pruned["pruned"] == 1 and not (tmp_path / "video" / "Night in.m3u8").exists()
    # One playlist by request.
    api.items_by_list["p-audio"] = [song_item("tc31", "Opener", 1)]
    api.lists[0]["Etag"] = "e3"
    assert api_playlists.apply(
        api, store, "p-audio", music_root=music_root, video_root=video_root
    )
    assert (tmp_path / "music" / "Gym.m3u8").read_text(encoding="utf-8").count(
        "#EXTINF"
    ) == 1


def test_nothing_is_written_without_the_setting_or_a_synced_library(
    store, tmp_path, monkeypatch
):
    api = FakeApi([{"Id": "p", "Name": "Gym", "MediaType": "Audio"}], {"p": []})
    root = tmp_path / "playlists"
    monkeypatch.setattr(api_playlists, "wanted", lambda: False)
    assert api_playlists.reconcile(api, store, music_root=str(root)) == {}
    monkeypatch.setattr(api_playlists, "wanted", lambda: True)
    assert api_playlists.reconcile(api, store, music_root=str(root)) == {}
    assert not root.exists()


def test_the_playlists_follow_the_presentation_switch(store, tmp_path, monkeypatch):
    """One setting for the node tree and every playlist (sync/dynamic.py)."""
    from kofin.sync import dynamic
    from tests.unit.fakes import FakeAddon

    synced(store, (LIB, "music"))
    api = FakeApi([{"Id": "p", "Name": "Gym", "MediaType": "Audio"}], {"p": []})
    root = tmp_path / "playlists"
    assert api_playlists.SETTING == dynamic.SETTING == "libraryNodes"
    FakeAddon.store = {dynamic.SETTING: "false", "deviceId": "d" * 32}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    assert api_playlists.wanted() is False
    assert api_playlists.reconcile(api, store, music_root=str(root)) == {}
    assert api_playlists.apply(api, store, "p", music_root=str(root)) is False
    assert not root.exists()


def test_a_side_whose_last_library_left_loses_its_playlists(
    store, backend, kodi, tmp_path, monkeypatch
):
    from kofin.sync import kofindb

    monkeypatch.setattr(api_playlists, "wanted", lambda: True)
    backend.setup()
    store.publish(album_bundle(songs=1), library=LIB)
    backend.reconcile()
    synced(store, (LIB, "music"))
    api = FakeApi(
        [{"Id": "p", "Name": "Gym", "MediaType": "Audio", "Etag": "1"}],
        {"p": [song_item("tc31", "One", 1)]},
    )
    root = tmp_path / "playlists" / "music"
    assert api_playlists.reconcile(api, store, music_root=str(root))["written"] == 1
    assert (root / "Gym.m3u8").is_file()
    synced(store)  # the last library leaves the selection
    assert api_playlists.reconcile(api, store, music_root=str(root)) == {"pruned": 1}
    assert not root.exists() or not list(root.glob("*.m3u8"))
    with private.Database() as db:
        assert kofindb.JellyfinDatabase(db.cursor).get_playlist_states() == []
    # Nothing left to prune: the next pass is quiet.
    assert api_playlists.reconcile(api, store, music_root=str(root)) == {}


def test_a_write_the_catalogue_left_short_is_resolved_again_next_pass(
    store, backend, kodi, tmp_path, monkeypatch
):
    """The server's Etag does not move when Kodi files an item the pass had
    not filed yet, so a short write withholds its checksum."""
    monkeypatch.setattr(api_playlists, "wanted", lambda: True)
    backend.setup()
    store.publish(album_bundle(songs=1), library=LIB)
    backend.reconcile()
    synced(store, (LIB, "music"))
    api = FakeApi(
        [{"Id": "p", "Name": "Gym", "MediaType": "Audio", "Etag": "same"}],
        {"p": [song_item("tc31", "One", 1), song_item("tc32", "Two", 2)]},
    )
    root = tmp_path / "playlists" / "music"
    stats = api_playlists.reconcile(api, store, music_root=str(root))
    assert (stats["tracks"], stats["skipped"]) == (1, 1)
    # The second song reaches Kodi; the Etag is unchanged.
    store.publish(album_bundle(songs=2), library=LIB)
    backend.reconcile()
    stats = api_playlists.reconcile(api, store, music_root=str(root))
    assert (stats["tracks"], stats["skipped"]) == (2, 0)
    assert (root / "Gym.m3u8").read_text(encoding="utf-8").count("#EXTINF") == 2
    # Complete, the Etag skips it.
    assert api_playlists.reconcile(api, store, music_root=str(root))["tracks"] == 0


def test_the_switch_commands_leave_the_files_to_the_playlists_pass(store, monkeypatch):
    """Both commands are queued by the switch moving and only mark the pass
    due, which reads the switch when it runs: a cleanup queued before the
    switch went back on takes nothing, and a pass that cannot read it stays
    owed until it can."""
    from tests.unit.apifixtures import Server, worker

    w = worker(store, Server({LIB: []}), [LIB], monkeypatch)
    switch = [None]
    ran = []
    monkeypatch.setattr(api_playlists, "wanted", lambda: switch[0])
    monkeypatch.setattr(w, "reconcile_subscriptions", lambda: ran.append("subs"))
    w._playlists_due = False
    w.command("CleanupMusicPlaylists", None)
    assert w._playlists_due
    w.playlists_pass()
    assert w._playlists_due and ran == []
    switch[0] = True
    w.playlists_pass()
    assert not w._playlists_due and ran == ["subs"]
    w.command("SyncMusicPlaylists", None)
    assert w._playlists_due


def test_a_named_playlist_the_switch_cannot_say_about_is_owed_to_the_pass(
    store, monkeypatch
):
    """A playlist the server saved while the settings cannot be read is
    written by the pass once they can; off, it writes nothing and owes
    nothing."""
    from tests.unit.apifixtures import Server, worker

    w = worker(store, Server({LIB: []}), [LIB], monkeypatch)
    asked = []
    monkeypatch.setattr(w, "reconcile_subscriptions", lambda i="": asked.append(i))
    for switch, owed in ((None, True), (False, False)):
        monkeypatch.setattr(api_playlists, "wanted", lambda switch=switch: switch)
        assert api_playlists.apply(None, store, "p") is switch
        w._playlists_due = False
        w.command("SyncPlaylists", {"Id": "p"})
        assert w._playlists_due is owed
    assert asked == ["p", "p"]


def test_with_the_switch_off_the_pass_takes_both_sides_down(
    store, backend, kodi, tmp_path, monkeypatch
):
    """Off, the pass removes the Jellyfin playlists of both sides and their
    states, whatever queued it; the Downloaded-music view and the smart
    playlists, which are the tree's, stay."""
    from kofin.sync import playlists

    monkeypatch.setattr(api_playlists, "wanted", lambda: True)
    backend.setup()
    store.publish(album_bundle(songs=1), library=LIB)
    backend.reconcile()
    synced(store, (LIB, "music"))
    api = FakeApi(
        [{"Id": "p", "Name": "Gym", "MediaType": "Audio", "Etag": "e"}],
        {"p": [song_item("tc31", "One", 1)]},
    )
    music = tmp_path / "playlists" / "music"
    video = tmp_path / "playlists" / "video"
    video.mkdir(parents=True)
    api_playlists.reconcile(api, store, music_root=str(music), video_root=str(video))
    assert (music / "Gym.m3u8").is_file()
    (music / playlists.DOWNLOADED_MUSIC_M3U8).write_text("#EXTM3U\n")
    (video / "Films.m3u8").write_text("#EXTM3U\n")
    (video / "kofinmovies1.xsp").write_text("<smartplaylist/>")
    assert not api_playlists.owed()
    monkeypatch.setattr(api_playlists, "wanted", lambda: False)
    assert api_playlists.owed()
    stats = api_playlists.reconcile(
        None, store, music_root=str(music), video_root=str(video)
    )
    assert stats == {"pruned": 2}
    assert not (music / "Gym.m3u8").exists()
    assert (music / playlists.DOWNLOADED_MUSIC_M3U8).is_file()
    assert [p.name for p in video.iterdir()] == ["kofinmovies1.xsp"]
    assert not api_playlists.owed()
    monkeypatch.setattr(api_playlists, "wanted", lambda: None)
    assert api_playlists.reconcile(None, store) is None


def test_a_playlist_without_an_etag_is_resolved_on_every_pass(
    store, backend, kodi, tmp_path, monkeypatch
):
    """An empty checksum never skips: it is the dirty mark of a short write,
    and what a playlist the server gave no Etag stores."""
    monkeypatch.setattr(api_playlists, "wanted", lambda: True)
    backend.setup()
    store.publish(album_bundle(songs=1), library=LIB)
    backend.reconcile()
    synced(store, (LIB, "music"))
    api = FakeApi(
        [{"Id": "p", "Name": "Gym", "MediaType": "Audio"}],
        {"p": [song_item("tc31", "One", 1)]},
    )
    root = tmp_path / "playlists" / "music"
    assert api_playlists.reconcile(api, store, music_root=str(root))["tracks"] == 1
    assert api_playlists.reconcile(api, store, music_root=str(root))["tracks"] == 1


def test_playlist_lines_name_their_items_through_the_catalogue(
    store, backend, kodi, monkeypatch
):
    """Save playlist to Jellyfin reads a Kodi playlist file back to items: the
    URL Kodi filed a row under carries its id, a library path names the row,
    and the catalogue says whether the item is a song or a video."""
    from kofin.sync.backends.api import identity

    backend.setup()
    store.publish([movie()], library=LIB)
    store.publish(album_bundle(songs=2), library=LIB2)
    backend.reconcile()
    monkeypatch.setattr(
        identity,
        "mapped_item",
        lambda kodi_id, media: "tc32" if media == "song" else None,
    )
    resolver = api_playlists.Resolver(store)
    lines = [
        resolver.entry({"Id": "a", "Type": "Movie"}, "Video").path,
        resolver.entry(song_item("tc31", "Opener", 1), "Audio").path,
        "musicdb://songs/7.flac",
        paths.playback_url(store.namespace, "Movie", LIB, "zz"),
        "/home/user/Music/local.mp3",
    ]
    assert api_playlists.items_of_lines(store, lines) == [
        ("a", "Video"),
        ("tc31", "Audio"),
        ("tc32", "Audio"),
        ("", ""),
        ("", ""),
    ]


def test_save_playlist_to_jellyfin_sends_the_songs_kofin_filed(
    store, backend, kodi, tmp_path, monkeypatch
):
    from kofin.plugin import actions
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import identity

    backend.setup()
    store.publish(album_bundle(songs=2), library=LIB2)
    backend.reconcile()
    resolver = api_playlists.Resolver(store)
    songs = [
        resolver.entry(song_item(item_id, "Song", track), "Audio").path
        for track, item_id in enumerate(("tc31", "tc32"), 1)
    ]
    playlist = tmp_path / "Mine.m3u8"
    playlist.write_text(
        "#EXTM3U\n" + "\n".join(songs) + "\n/home/user/Music/local.mp3\n",
        encoding="utf-8",
    )
    created = []

    class Api:
        def create_playlist(self, name, ids, media_type):
            created.append((name, ids, media_type))
            return {"Id": "p" * 32}

    notified = []
    monkeypatch.setattr(identity, "current_store", lambda: store)
    monkeypatch.setattr(actions, "_api", lambda: Api())
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: path)
    monkeypatch.setattr(
        actions.ipc, "notify", lambda kind, data: notified.append((kind, data))
    )
    actions.save_playlist(
        Request("plugin://plugin.video.kofin/", 1, {"path": str(playlist)})
    )
    assert created == [("Mine", ["tc31", "tc32"], "Audio")]
    assert notified == [(actions.ipc.SYNC_PLAYLISTS, {"Id": "p" * 32})]


def test_a_service_that_starts_with_the_switch_off_owes_the_playlists_pass(
    store, monkeypatch
):
    """The applier hears no change for a stored default Kodi moved as it
    loaded the settings, so the library thread asks at its start."""
    from types import SimpleNamespace

    from kofin.sync.backends.api import library
    from tests.unit.apifixtures import Server, worker

    w = worker(store, Server({LIB: []}), [LIB], monkeypatch)
    native = SimpleNamespace(setup=lambda: None, restore_settings=lambda: None)
    monkeypatch.setattr(library, "Native", lambda *a, **k: native)
    w._bar = lambda: None
    w.stop_thread = True
    for owed in (True, False):
        monkeypatch.setattr(api_playlists, "owed", lambda owed=owed: owed)
        w._run(SimpleNamespace(abortRequested=lambda: True))
        assert w._playlists_due is owed
