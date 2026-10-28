"""Unit tests for one-way music playlist materialization."""

import os
from types import SimpleNamespace

import pytest

from kofin.sync import playlists

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture(autouse=True)
def addon_media(monkeypatch):
    """Point ``addon_path`` at the checkout, so a refresh copies the icon it
    really ships the way it does in Kodi -- and fails here if that asset ever
    moves."""
    monkeypatch.setattr(playlists.settings, "addon_path", lambda: REPO_ROOT)


class FakeApi:
    def __init__(self, playlist_list=None, items_by_id=None):
        self._playlists = playlist_list or []
        self._items = items_by_id or {}
        self.item_requests = []

    def music_playlists(self):
        return list(self._playlists)

    def playlist_items(self, playlist_id, start_index=0, limit=100):
        self.item_requests.append((playlist_id, start_index, limit))
        all_items = self._items.get(playlist_id, [])
        page = all_items[start_index : start_index + limit]
        # Some builds over-report the count; the caller must not trust it.
        return {
            "Items": page,
            "TotalRecordCount": len(all_items) + (5 if all_items else 0),
        }


def test_oversized_playlist_never_becomes_a_partial_membership_snapshot():
    class OversizedApi:
        def playlist_items(self, playlist_id, start_index=0, limit=100):
            return {
                "Items": [{"Id": "first", "Type": "Audio"}],
                "TotalRecordCount": playlists.MAX_PLAYLIST_ITEMS + 1,
            }

    with pytest.raises(ValueError, match="exceeds"):
        playlists._iter_playlist_items(OversizedApi(), "large")


@pytest.mark.parametrize(
    "body", [{}, {"Items": [], "TotalRecordCount": 1}, {"Items": [None]}]
)
def test_incomplete_playlist_page_is_not_an_empty_membership(body):
    class Api:
        def playlist_items(self, playlist_id, start_index=0, limit=100):
            return body

    with pytest.raises(ValueError):
        playlists._iter_playlist_items(Api(), "mix")


class FakeMapping:
    def __init__(self, rows):
        # jellyfin_id -> Row-like with media_type, kodi_id
        self._rows = rows

    def get_item_by_id(self, jellyfin_id):
        return self._rows.get(jellyfin_id)


class FakeState:
    def __init__(self):
        self._rows = {}

    def get_playlist_state(self, jellyfin_id):
        return self._rows.get(jellyfin_id)

    def add_playlist_state(self, jellyfin_id, media_type, filename, checksum):
        self._rows[jellyfin_id] = (media_type, filename, checksum)

    def remove_playlist_state(self, jellyfin_id):
        self._rows.pop(jellyfin_id, None)

    def get_playlist_states(self):
        return [
            (jellyfin_id, row[0], row[1], row[2])
            for jellyfin_id, row in self._rows.items()
        ]


def test_safe_filename_keeps_unicode_and_strips_slashes():
    assert playlists.safe_filename("Road Trip") == "Road Trip"
    assert playlists.safe_filename("a/b\\c") == "a_b_c"
    assert playlists.safe_filename("  ") == "playlist"
    assert playlists.safe_filename("Gym: 2024") == "Gym_ 2024"
    assert playlists.safe_filename("Ångström") == "Ångström"


def test_entry_label_matches_kodis_own_format():
    # What Kodi writes saving a library playlist: "01. AC/DC - Jailbreak".
    entry = playlists.Entry("p", "Jailbreak", artist="AC/DC", track=1, duration=280)
    assert playlists.entry_label(entry) == "01. AC/DC - Jailbreak"
    # Degrades a field at a time rather than leaving stray separators.
    assert playlists.entry_label(playlists.Entry("p", "Solo")) == "Solo"
    assert playlists.entry_label(playlists.Entry("p", "Solo", artist="A")) == "A - Solo"
    assert playlists.entry_label(playlists.Entry("p", "", track=3)) == "03."


def test_render_m3u8_states_duration_and_label():
    text = playlists.render_m3u8(
        [
            playlists.Entry("http://s/a", "Song A", artist="A", track=1, duration=200),
            playlists.Entry("http://s/b", "Song B"),
        ]
    )
    assert text.startswith("#EXTM3U\n")
    assert "#EXTINF:200,01. A - Song A\nhttp://s/a\n" in text
    # No duration in the row: the m3u "unknown" value, not a bogus zero.
    assert "#EXTINF:-1,Song B\nhttp://s/b\n" in text


def test_cleanup_removes_folder(tmp_path):
    root = tmp_path / "Kofin"
    root.mkdir()
    (root / "A.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    removed = playlists.cleanup_managed_playlists(root=str(root))
    assert removed == 1
    assert not root.exists()


# --- which path form a row gets ----------------------------------------------


def test_playlist_side_skips_mixed_and_unknown():
    audio = [{"Type": "Audio"}]
    video = [{"Type": "Movie"}]
    mixed = [{"Type": "Audio"}, {"Type": "Movie"}]
    assert playlists.playlist_side("Audio", audio) == "Audio"
    assert playlists.playlist_side("Video", video) == "Video"
    assert playlists.playlist_side("Audio", mixed) is None
    assert playlists.playlist_side("Unknown", audio) is None
    assert playlists.playlist_side("", audio) is None
    assert playlists.playlist_side("Audio", []) == "Audio"


def test_enabled_kinds_follow_synced_libraries():
    views = [
        SimpleNamespace(view_id="m1", media_type="music"),
        SimpleNamespace(view_id="v1", media_type="movies"),
        SimpleNamespace(view_id="t1", media_type="tvshows"),
    ]
    assert playlists.enabled_kinds(views, {"m1"}) == {"Audio"}
    assert playlists.enabled_kinds(views, {"v1"}) == {"Video"}
    assert playlists.enabled_kinds(views, {"m1", "t1"}) == {"Audio", "Video"}
    assert playlists.enabled_kinds(views, set()) == set()


def test_parse_m3u_skips_headers():
    text = "#EXTM3U\n#EXTINF:1,A\nhttp://a\n#comment\nhttp://b\n"
    assert playlists.parse_m3u_paths(text) == ["http://a", "http://b"]


PLUGIN_DIR = "plugin://plugin.video.kofin/lib/"
PLUGIN_FILE = (
    PLUGIN_DIR + "?filename=Aliens.mkv&id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "&dbid=1128&mode=play"
)


def _audio_playlist(name, etag, playlist_id="pl1"):
    return {
        "Id": playlist_id,
        "Name": name,
        "MediaType": "Audio",
        "Etag": etag,
    }


# --- the managed folder's own icon -------------------------------------------


def test_folder_icon_is_not_rewritten_once_it_is_there(tmp_path):
    """It rides the playlist poll, so a rewrite every pass would churn the
    folder's mtime for nothing."""
    root = tmp_path / "Kofin"
    root.mkdir()
    assert playlists.write_folder_icon(str(root)) is True
    stamped = (root / playlists.FOLDER_ICON).stat().st_mtime_ns

    assert playlists.write_folder_icon(str(root)) is False
    assert (root / playlists.FOLDER_ICON).stat().st_mtime_ns == stamped


def test_folder_icon_is_replaced_when_the_shipped_one_changes(tmp_path):
    root = tmp_path / "Kofin"
    root.mkdir()
    (root / playlists.FOLDER_ICON).write_bytes(b"stale")

    assert playlists.write_folder_icon(str(root)) is True
    assert (root / playlists.FOLDER_ICON).read_bytes()[:4] == b"\x89PNG"


def test_cleanup_takes_the_icon_with_the_folder(tmp_path):
    root = tmp_path / "Kofin"
    root.mkdir()
    playlists.write_folder_icon(str(root))
    (root / "A.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

    assert playlists.cleanup_managed_playlists(root=str(root)) == 2
    assert not root.exists()


# -- the Downloaded-music view -------------------------------------------------


def test_cleanup_without_the_view_still_takes_everything(tmp_path):
    root = tmp_path / "Kofin"
    root.mkdir()
    (root / "A.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (root / playlists.FOLDER_ICON).write_bytes(b"png")
    assert playlists.cleanup_managed_playlists(root=str(root)) == 2
    assert not root.exists()


def test_the_downloaded_music_list_keeps_its_name_and_survives_the_cleanup(tmp_path):
    """The Downloaded-music list sits beside the server's playlists: a server
    playlist of the same title takes "(2)", even one that held the name
    before, and the cleanup and the prune leave it."""
    from kofin.sync import playlists as pl

    assert (
        pl.assign_playlist_filename(
            "Downloaded music", set(pl.RESERVED_STEMS), "Downloaded music.m3u8"
        )
        == "Downloaded music (2).m3u8"
    )
    root = tmp_path / "Kofin"
    root.mkdir()
    (root / pl.DOWNLOADED_MUSIC_M3U8).write_text("#EXTM3U\n")
    (root / "Mix.m3u8").write_text("#EXTM3U\n")
    assert "downloaded music" in pl._m3u8_stems(str(root))
    pl.cleanup_managed_playlists(str(root))
    assert sorted(p.name for p in root.iterdir()) == [pl.DOWNLOADED_MUSIC_M3U8]


# --- the entries come from a resolver (backends/api/playlists.Resolver) ------


class FakeResolver:
    """``entry(item, side)``: what the catalogue answers for a filed item."""

    def __init__(self, entries):
        self._entries = entries

    def entry(self, item, side):
        return self._entries.get((item.get("Id"), side))


SONG_LINE = "musicdb://songs/10.flac"


def _audio_resolver():
    return FakeResolver(
        {("a1", "Audio"): playlists.Entry(SONG_LINE, "Track", "A", 1, 60)}
    )


def _video_resolver():
    return FakeResolver({("m1", "Video"): playlists.Entry(PLUGIN_FILE, "Aliens")})


def test_failed_playlist_page_keeps_its_file_and_does_not_stop_others(tmp_path):
    class Api(FakeApi):
        def playlist_items(self, playlist_id, start_index=0, limit=100):
            if playlist_id == "bad":
                return {}
            return super().playlist_items(playlist_id, start_index, limit)

    root = tmp_path / "Kofin"
    root.mkdir()
    (root / "Old.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    state = FakeState()
    state.add_playlist_state("bad", "Audio", "Old.m3u8", "old")
    api = Api(
        playlist_list=[
            _audio_playlist("Old", "new", playlist_id="bad"),
            _audio_playlist("Gym", "new", playlist_id="good"),
        ],
        items_by_id={"good": [{"Id": "a1", "Type": "Audio"}]},
    )
    playlists.reconcile(
        api,
        state,
        {"Audio"},
        music_root=str(root),
        resolver=_audio_resolver(),
    )
    assert (root / "Old.m3u8").is_file()
    assert state.get_playlist_state("bad") is not None
    assert SONG_LINE in (root / "Gym.m3u8").read_text(encoding="utf-8")


def test_apply_one_keeps_the_filename_when_membership_changes(tmp_path):
    """A membership rewrite must overwrite Name.m3u8, not mint Name (2).m3u8,
    and a plugin row's line is the file name alone (video_playlist_line)."""
    api = FakeApi(items_by_id={"pl1": [{"Id": "m1", "Type": "Movie"}]})
    state = FakeState()
    playlist = {"Id": "pl1", "Name": "UHD", "MediaType": "Video", "Etag": "one"}
    root = str(tmp_path)
    resolver = _video_resolver()
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Video"},
        video_root=root,
        resolver=resolver,
    )
    assert os.path.isfile(os.path.join(root, "UHD.m3u8"))
    assert not os.path.isfile(os.path.join(root, "UHD (2).m3u8"))
    text = open(os.path.join(root, "UHD.m3u8"), encoding="utf-8").read()
    assert PLUGIN_FILE in text
    assert PLUGIN_DIR + PLUGIN_FILE not in text

    playlist["Etag"] = "two"
    api._items["pl1"] = [{"Id": "m1", "Type": "Movie"}, {"Id": "m1", "Type": "Movie"}]
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Video"},
        video_root=root,
        resolver=resolver,
    )
    assert os.path.isfile(os.path.join(root, "UHD.m3u8"))
    assert not os.path.isfile(os.path.join(root, "UHD (2).m3u8"))
    assert state.get_playlist_state("pl1")[1] == "UHD.m3u8"


def test_apply_one_does_not_steal_another_playlist_filename(tmp_path):
    other = tmp_path / "UHD.m3u8"
    other.write_text("#EXTM3U\n", encoding="utf-8")
    api = FakeApi(items_by_id={"pl2": [{"Id": "m1", "Type": "Movie"}]})
    state = FakeState()
    playlist = {"Id": "pl2", "Name": "UHD", "MediaType": "Video", "Etag": "one"}
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Video"},
        video_root=str(tmp_path),
        resolver=_video_resolver(),
    )
    assert os.path.isfile(str(other))
    assert os.path.isfile(str(tmp_path / "UHD (2).m3u8"))
    assert state.get_playlist_state("pl2")[1] == "UHD (2).m3u8"


def test_apply_one_keeps_a_music_filename_when_membership_changes(tmp_path):
    """Same rule as the video rewrite: a track added must not mint Gym (2).m3u8."""
    api = FakeApi(items_by_id={"pl1": [{"Id": "a1", "Type": "Audio"}]})
    state = FakeState()
    playlist = _audio_playlist("Gym", "one")
    root = str(tmp_path)
    resolver = _audio_resolver()
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Audio"},
        music_root=root,
        resolver=resolver,
    )
    assert os.path.isfile(os.path.join(root, "Gym.m3u8"))

    playlist["Etag"] = "two"
    api._items["pl1"] = [{"Id": "a1", "Type": "Audio"}, {"Id": "a1", "Type": "Audio"}]
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Audio"},
        music_root=root,
        resolver=resolver,
    )
    assert os.path.isfile(os.path.join(root, "Gym.m3u8"))
    assert not os.path.isfile(os.path.join(root, "Gym (2).m3u8"))
    assert state.get_playlist_state("pl1")[1] == "Gym.m3u8"


def test_apply_one_keeps_a_disambiguated_music_filename(tmp_path):
    """``Gym (2).m3u8`` stays put when the base name is free.

    Recomputing the stem would rename it to ``Gym.m3u8`` and, on the next
    event, bounce it back if ``Gym.m3u8`` reappears.
    """
    api = FakeApi(items_by_id={"pl2": [{"Id": "a1", "Type": "Audio"}]})
    state = FakeState()
    state.add_playlist_state(
        "pl2", "Audio", "Gym (2).m3u8", playlists.reference_checksum("old")
    )
    (tmp_path / "Gym (2).m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    playlist = _audio_playlist("Gym", "new", playlist_id="pl2")
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Audio"},
        music_root=str(tmp_path),
        resolver=_audio_resolver(),
    )
    assert os.path.isfile(str(tmp_path / "Gym (2).m3u8"))
    assert not os.path.isfile(str(tmp_path / "Gym.m3u8"))
    assert state.get_playlist_state("pl2")[1] == "Gym (2).m3u8"


def test_reconcile_keeps_a_disambiguated_music_filename_on_update(tmp_path):
    """The changed playlist is listed first, so the base name is not reserved yet."""
    (tmp_path / "Gym.m3u8").write_text("#EXTM3U\nowned\n", encoding="utf-8")
    (tmp_path / "Gym (2).m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    state = FakeState()
    state.add_playlist_state(
        "pl-base", "Audio", "Gym.m3u8", playlists.reference_checksum("same")
    )
    state.add_playlist_state(
        "pl-two", "Audio", "Gym (2).m3u8", playlists.reference_checksum("old")
    )
    api = FakeApi(
        playlist_list=[
            _audio_playlist("Gym", "new", playlist_id="pl-two"),
            _audio_playlist("Gym", "same", playlist_id="pl-base"),
        ],
        items_by_id={
            "pl-two": [{"Id": "a1", "Type": "Audio"}],
            "pl-base": [{"Id": "a1", "Type": "Audio"}],
        },
    )
    playlists.reconcile(
        api,
        state,
        {"Audio"},
        music_root=str(tmp_path),
        resolver=_audio_resolver(),
    )
    assert state.get_playlist_state("pl-two")[1] == "Gym (2).m3u8"
    assert state.get_playlist_state("pl-base")[1] == "Gym.m3u8"
    assert (tmp_path / "Gym.m3u8").read_text(encoding="utf-8") == "#EXTM3U\nowned\n"
    assert (tmp_path / "Gym (2).m3u8").is_file()
    rewritten = (tmp_path / "Gym (2).m3u8").read_text(encoding="utf-8")
    assert SONG_LINE in rewritten
    assert "owned" not in rewritten


def test_apply_one_renames_a_music_playlist_when_the_title_changes(tmp_path):
    api = FakeApi(items_by_id={"pl1": [{"Id": "a1", "Type": "Audio"}]})
    state = FakeState()
    playlist = _audio_playlist("Gym", "one")
    root = str(tmp_path)
    resolver = _audio_resolver()
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Audio"},
        music_root=root,
        resolver=resolver,
    )
    playlist["Name"] = "Road Trip"
    playlist["Etag"] = "two"
    playlists.apply_one(
        api,
        state,
        playlist,
        {"Audio"},
        music_root=root,
        resolver=resolver,
    )
    assert not os.path.isfile(os.path.join(root, "Gym.m3u8"))
    assert os.path.isfile(os.path.join(root, "Road Trip.m3u8"))
    assert state.get_playlist_state("pl1")[1] == "Road Trip.m3u8"


def test_an_empty_listing_does_not_empty_the_managed_folder(tmp_path):
    """The managed folder's ownership rule is the folder itself, so every
    .m3u8 not in the server's set is removed. A listing that comes back empty
    for any reason other than "the user has no playlists" would wipe the lot
    -- a permission change and an empty account look identical from here --
    so the prune stands down while playlists stand."""
    root = str(tmp_path / "Kofin")
    state = FakeState()
    stocked = FakeApi(
        playlist_list=[_audio_playlist("Gym", "one")],
        items_by_id={"pl1": [{"Id": "a1", "Type": "Audio", "Name": "T"}]},
    )
    playlists.reconcile(
        stocked,
        state,
        {"Audio"},
        music_root=root,
        resolver=_audio_resolver(),
    )
    directory = playlists.managed_dir(root)
    written = [n for n in os.listdir(directory) if n.endswith(".m3u8")]
    assert written, "the fixture did not write a playlist to prune"

    stats = playlists.reconcile(
        FakeApi(playlist_list=[]),
        state,
        {"Audio"},
        music_root=root,
        resolver=_audio_resolver(),
    )
    assert stats["pruned"] == 0
    assert [n for n in os.listdir(directory) if n.endswith(".m3u8")] == written


def test_a_server_playlist_on_the_reserved_name_moves_off_it_and_leaves_the_view(
    tmp_path,
):
    """A server playlist synced as "Downloaded music.m3u8" before the stem
    was reserved moves to "(2)" on its next pass, its Etag unchanged or not,
    and never takes the download list with it: the list writes the same file,
    so the old name goes only while it is still the playlist's own copy."""
    from kofin.sync import playlists as pl

    view = "#EXTM3U\ndownloaded\n"
    playlist = _audio_playlist("Downloaded music", "same")
    api = FakeApi(
        playlist_list=[playlist], items_by_id={"pl1": [{"Id": "a1", "Type": "Audio"}]}
    )
    resolver = _audio_resolver()
    moved = "Downloaded music (2).m3u8"
    for run in ("reconcile", "apply_one"):
        root = tmp_path / run
        root.mkdir()
        (root / pl.DOWNLOADED_MUSIC_M3U8).write_text(view, encoding="utf-8")
        state = FakeState()
        state.add_playlist_state(
            "pl1", "Audio", pl.DOWNLOADED_MUSIC_M3U8, pl.reference_checksum("same")
        )
        if run == "reconcile":
            pl.reconcile(api, state, {"Audio"}, str(root), resolver=resolver)
        else:
            pl.apply_one(
                api,
                state,
                playlist,
                {"Audio"},
                str(root),
                resolver=resolver,
            )
        assert state.get_playlist_state("pl1")[1] == moved
        assert SONG_LINE in (root / moved).read_text(encoding="utf-8")
        assert (root / pl.DOWNLOADED_MUSIC_M3U8).read_text(encoding="utf-8") == view
    # No list was ever written over it: the old name is the playlist's copy.
    root = tmp_path / "copy"
    root.mkdir()
    state = FakeState()
    state.add_playlist_state(
        "pl1", "Audio", pl.DOWNLOADED_MUSIC_M3U8, pl.reference_checksum("same")
    )
    pl.apply_one(
        api,
        state,
        playlist,
        {"Audio"},
        str(root),
        resolver=resolver,
    )
    (root / moved).rename(root / pl.DOWNLOADED_MUSIC_M3U8)
    state.add_playlist_state(
        "pl1", "Audio", pl.DOWNLOADED_MUSIC_M3U8, pl.reference_checksum("same")
    )
    pl.apply_one(
        api,
        state,
        playlist,
        {"Audio"},
        str(root),
        resolver=resolver,
    )
    assert sorted(p.name for p in root.glob("*.m3u8")) == [moved]
