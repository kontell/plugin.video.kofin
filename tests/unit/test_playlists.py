"""Unit tests for one-way music playlist materialization."""

from types import SimpleNamespace

from kofin.sync import playlists


class FakeApi:
    def __init__(self, playlist_list=None, items_by_id=None):
        self._playlists = playlist_list or []
        self._items = items_by_id or {}

    def music_playlists(self):
        return list(self._playlists)

    def playlist_items(self, playlist_id, start_index=0, limit=100):
        all_items = self._items.get(playlist_id, [])
        page = all_items[start_index : start_index + limit]
        return {"Items": page, "TotalRecordCount": len(all_items)}


class FakeMapping:
    def __init__(self, rows):
        # jellyfin_id -> Row-like with media_type, kodi_id
        self._rows = rows

    def get_item_by_id(self, jellyfin_id):
        return self._rows.get(jellyfin_id)


class FakeMusic:
    def __init__(self, songs):
        # kodi song id -> (strPath, strFileName, strTitle)
        self._songs = songs

    def get_song_path_filename(self, song_id):
        return self._songs.get(song_id)


def test_safe_filename_keeps_unicode_and_strips_slashes():
    assert playlists.safe_filename("Road Trip") == "Road Trip"
    assert playlists.safe_filename("a/b\\c") == "a_b_c"
    assert playlists.safe_filename("  ") == "playlist"
    assert playlists.safe_filename("Gym: 2024") == "Gym_ 2024"
    assert playlists.safe_filename("Ångström") == "Ångström"


def test_join_song_path():
    assert (
        playlists.join_song_path("http://s/Audio/id/", "stream.mp3?static=true")
        == "http://s/Audio/id/stream.mp3?static=true"
    )
    assert (
        playlists.join_song_path(
            "plugin://plugin.video.kofin/lib/id/",
            "stream.mp3?mode=play&id=id",
        )
        == "plugin://plugin.video.kofin/lib/id/stream.mp3?mode=play&id=id"
    )
    # tolerate missing trailing slash
    assert playlists.join_song_path("http://s/Audio/id", "stream.flac") == (
        "http://s/Audio/id/stream.flac"
    )


def test_render_m3u8():
    text = playlists.render_m3u8([("Song A", "http://s/a"), ("Song B", "http://s/b")])
    assert text.startswith("#EXTM3U\n")
    assert "#EXTINF:-1,Song A\nhttp://s/a\n" in text
    assert "#EXTINF:-1,Song B\nhttp://s/b\n" in text


def test_refresh_writes_server_names_and_paths(tmp_path):
    api = FakeApi(
        playlist_list=[
            {"Id": "pl1", "Name": "Gym", "MediaType": "Audio"},
            {"Id": "pl2", "Name": "Road Trip", "MediaType": "Audio"},
        ],
        items_by_id={
            "pl1": [
                {"Id": "a1", "Type": "Audio", "Name": "Track 1"},
                {"Id": "missing", "Type": "Audio", "Name": "Gone"},
            ],
            "pl2": [
                {"Id": "a2", "Type": "Audio", "Name": "Track 2"},
            ],
        },
    )
    mapping = FakeMapping(
        {
            "a1": SimpleNamespace(media_type="song", kodi_id=10),
            "a2": SimpleNamespace(media_type="song", kodi_id=20),
        }
    )
    music = FakeMusic(
        {
            10: ("http://s/Audio/a1/", "stream.mp3?static=true", "Track 1"),
            20: ("http://s/Audio/a2/", "stream.flac?static=true", "Track 2"),
        }
    )

    root = str(tmp_path / "Kofin")
    stats = playlists.refresh_music_playlists(api, mapping, music, root=root)

    assert stats["playlists"] == 2
    assert stats["tracks"] == 2
    assert stats["skipped"] == 1
    assert stats["pruned"] == 0

    gym = (tmp_path / "Kofin" / "Gym.m3u8").read_text(encoding="utf-8")
    assert "Track 1" in gym
    assert "http://s/Audio/a1/stream.mp3?static=true" in gym
    assert "Gone" not in gym

    trip = (tmp_path / "Kofin" / "Road Trip.m3u8").read_text(encoding="utf-8")
    assert "http://s/Audio/a2/stream.flac?static=true" in trip


def test_refresh_prunes_removed_and_handles_rename(tmp_path):
    root = tmp_path / "Kofin"
    root.mkdir()
    (root / "Old Name.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (root / "stale.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

    api = FakeApi(
        playlist_list=[{"Id": "pl1", "Name": "New Name", "MediaType": "Audio"}],
        items_by_id={"pl1": []},
    )
    stats = playlists.refresh_music_playlists(
        api, FakeMapping({}), FakeMusic({}), root=str(root)
    )

    assert stats["playlists"] == 1
    assert stats["pruned"] == 2
    names = sorted(p.name for p in root.iterdir())
    assert names == ["New Name.m3u8"]


def test_duplicate_names_disambiguated(tmp_path):
    api = FakeApi(
        playlist_list=[
            {"Id": "p1", "Name": "Mix", "MediaType": "Audio"},
            {"Id": "p2", "Name": "Mix", "MediaType": "Audio"},
        ],
        items_by_id={"p1": [], "p2": []},
    )
    playlists.refresh_music_playlists(
        api, FakeMapping({}), FakeMusic({}), root=str(tmp_path / "Kofin")
    )
    names = sorted(p.name for p in (tmp_path / "Kofin").iterdir())
    assert names == ["Mix (2).m3u8", "Mix.m3u8"]


def test_cleanup_removes_folder(tmp_path):
    root = tmp_path / "Kofin"
    root.mkdir()
    (root / "A.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    removed = playlists.cleanup_managed_playlists(root=str(root))
    assert removed == 1
    assert not root.exists()


def test_empty_playlist_writes_header_only(tmp_path):
    api = FakeApi(
        playlist_list=[{"Id": "empty", "Name": "Empty", "MediaType": "Audio"}],
        items_by_id={"empty": []},
    )
    playlists.refresh_music_playlists(
        api, FakeMapping({}), FakeMusic({}), root=str(tmp_path / "Kofin")
    )
    text = (tmp_path / "Kofin" / "Empty.m3u8").read_text(encoding="utf-8")
    assert text.strip() == "#EXTM3U"
