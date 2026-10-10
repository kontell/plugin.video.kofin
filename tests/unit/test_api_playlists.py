"""Jellyfin playlists as Kodi playlist files in the API build."""

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
    FakeAddon.store = {dynamic.SETTING: "false"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    assert api_playlists.wanted() is False
    assert api_playlists.reconcile(api, store, music_root=str(root)) == {}
    assert api_playlists.apply(api, store, "p", music_root=str(root)) is False
    assert not root.exists()
