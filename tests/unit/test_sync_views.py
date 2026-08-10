"""Views/nodes generation: file shapes, stock icons, and the views-hash
regeneration guard (plan §5 step 4)."""

import os
import pathlib

import pytest

from kofin.sync import db as sync_db
from kofin.sync import kofindb
from kofin.sync import kodisetup
from kofin.sync.views import (
    NODE_ROOT,
    NODE_ROOT_ICON,
    PLAYLIST_FOLDER,
    Views,
    node_icon,
)
from tests.unit.fakes import FakeAddon, FakeWindow


class FakeApi:
    server = "http://server:8096"
    user_id = "user1"

    def __init__(self, folders=None):
        self.folders = folders or []

    def media_folders(self):
        return {"Items": self.folders}

    def views(self):
        return {"Items": []}


@pytest.fixture(autouse=True)
def views_env(monkeypatch, tmp_path):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)

    profile = tmp_path / "profile"
    (profile / "library" / "video").mkdir(parents=True)
    (profile / "playlists" / "video").mkdir(parents=True)
    (profile / "addon_data" / "plugin.video.kofin").mkdir(parents=True)

    def translate(path):
        if path.startswith("special://profile"):
            rest = path.replace("special://profile", "").strip("/")
            return str(profile / rest) if rest else str(profile)
        return str(tmp_path / path.replace("special://", "").strip("/"))

    monkeypatch.setattr("xbmcvfs.translatePath", translate)
    monkeypatch.setattr("xbmcvfs.exists", lambda p: os.path.exists(translate_or_raw(p)))
    monkeypatch.setattr(
        "xbmcvfs.mkdir", lambda p: os.mkdir(translate_or_raw(p)) or True
    )
    monkeypatch.setattr(
        "xbmcvfs.mkdirs",
        lambda p: os.makedirs(translate_or_raw(p), exist_ok=True) or True,
    )
    monkeypatch.setattr(
        "xbmcvfs.delete", lambda p: os.remove(translate_or_raw(p)) or True
    )
    monkeypatch.setattr(
        "xbmcvfs.rmdir", lambda p: os.rmdir(translate_or_raw(p)) or True
    )

    def fake_listdir(p):
        target = translate_or_raw(p)
        dirs, files = [], []
        if os.path.isdir(target):
            for entry in os.listdir(target):
                if os.path.isdir(os.path.join(target, entry)):
                    dirs.append(entry)
                else:
                    files.append(entry)
        return dirs, files

    monkeypatch.setattr("xbmcvfs.listdir", fake_listdir)

    def translate_or_raw(path):
        return translate(path) if path.startswith("special://") else path

    sync_db.reset_overrides()
    sync_db.set_path_override(
        "kofin", str(profile / "addon_data" / "plugin.video.kofin" / "kofin.db")
    )
    yield {"profile": profile}
    sync_db.reset_overrides()


def seed(views, whitelist):
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        for view_id, name, media in views:
            mapping.add_view(view_id, name, media)
    sync = sync_db.get_sync()
    sync["Whitelist"] = list(whitelist)
    sync["SortedViews"] = [v[0] for v in views]
    sync_db.save_sync(sync)


def video_root(views_env):
    return views_env["profile"] / "library" / "video"


def kofin_root(views_env):
    return video_root(views_env) / NODE_ROOT


def playlists_root(views_env):
    """Kodi's own video playlist directory -- the user's."""
    return views_env["profile"] / "playlists" / "video"


def playlist_root(views_env):
    """The managed folder inside it -- ours."""
    return playlists_root(views_env) / PLAYLIST_FOLDER


def test_get_nodes_generates_files_with_stock_icons(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])

    Views(FakeApi()).get_nodes()

    node_dir = kofin_root(views_env) / "kofinmovieslib1"
    assert (node_dir / "index.xml").is_file()
    assert (node_dir / "all.xml").is_file()
    assert (node_dir / "recent.xml").is_file()

    index_xml = (node_dir / "index.xml").read_text()
    assert "<icon>DefaultMovies.png</icon>" in index_xml
    assert "icon.png" not in index_xml  # never addon art on structural entries

    recent_xml = (node_dir / "recent.xml").read_text()
    assert "<icon>DefaultRecentlyAddedMovies.png</icon>" in recent_xml
    assert "Movies" in recent_xml  # tag rule on the library name

    playlist = playlist_root(views_env) / "kofinmovieslib1.xsp"
    assert playlist.is_file()
    assert "<value>Movies</value>" in playlist.read_text()

    # Window props got populated.
    assert FakeWindow.store.get("Kofin.nodes.total")


def test_generated_nodes_live_under_one_kofin_parent(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])

    Views(FakeApi()).get_nodes()

    parent = kofin_root(views_env)
    assert (parent / "index.xml").is_file()
    assert (parent / "kofinmovieslib1").is_dir()
    assert (parent / "kofin_Favoritemovies.xml").is_file()

    # The one structural node that carries addon art (it *is* the addon), by a
    # special:// path so it survives being installed somewhere else.
    index_xml = (parent / "index.xml").read_text()
    assert "<icon>%s</icon>" % NODE_ROOT_ICON in index_xml
    assert NODE_ROOT_ICON.startswith("special://")

    # Nothing of ours is left loose in the video library root.
    assert not [
        entry for entry in os.listdir(video_root(views_env)) if entry != NODE_ROOT
    ]

    # Window prop paths point inside the parent, so the ActivateWindow
    # builtins a skin fires still resolve.
    assert FakeWindow.store["Kofin.nodes.0.content"].startswith(
        "library://video/%s/kofinmovieslib1/" % NODE_ROOT
    )


def test_parent_index_written_once_so_user_order_survives(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])
    Views(FakeApi()).get_nodes()

    index = kofin_root(views_env) / "index.xml"
    index.write_text('<node order="99"><label>Mine</label></node>')

    # Force a regeneration; the parent keeps the user's order and label.
    seed([("lib1", "Movies", "movies"), ("lib2", "Shows", "tvshows")], ["lib1", "lib2"])
    Views(FakeApi()).get_nodes()

    assert 'order="99"' in index.read_text()


def test_addon_string_node_labels_are_written_as_text(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])

    Views(FakeApi()).get_nodes()

    node_dir = kofin_root(views_env) / "kofinmovieslib1"
    # Ours resolve at generation time: a bare "30350" would be looked up in
    # Kodi's own strings, where the addon range is empty, and render blank.
    assert "<label>string-30350</label>" in (node_dir / "recent.xml").read_text()
    assert "<label>string-30352</label>" in (node_dir / "unwatched.xml").read_text()
    # Kodi-core ids stay numeric and keep following the UI language.
    assert "<label>135</label>" in (node_dir / "genres.xml").read_text()
    assert "<label>20434</label>" in (node_dir / "sets.xml").read_text()


def test_get_nodes_migrates_the_old_flat_layout(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])

    # Seed the pre-NODE_ROOT layout plus a hand-made node of the user's.
    old_dir = video_root(views_env) / "kofinmovieslib1"
    old_dir.mkdir()
    (old_dir / "index.xml").write_text("<node/>")
    (video_root(views_env) / "kofin_Favoritemovies.xml").write_text("<node/>")
    (video_root(views_env) / "movies").mkdir()
    (video_root(views_env) / "movies" / "syncplay.xml").write_text("<node/>")

    Views(FakeApi()).get_nodes()

    assert not old_dir.exists()
    assert not (video_root(views_env) / "kofin_Favoritemovies.xml").exists()
    assert (kofin_root(views_env) / "kofinmovieslib1" / "index.xml").is_file()
    # Never ours to delete.
    assert (video_root(views_env) / "movies" / "syncplay.xml").is_file()


def test_get_nodes_prunes_libraries_that_left_the_whitelist(views_env):
    seed([("lib1", "Movies", "movies"), ("lib2", "Shows", "tvshows")], ["lib1", "lib2"])
    Views(FakeApi()).get_nodes()
    assert (kofin_root(views_env) / "kofintvshowslib2").is_dir()

    # lib2 leaves by a route that never called remove_library.
    sync = sync_db.get_sync()
    sync["Whitelist"] = ["lib1"]
    sync_db.save_sync(sync)
    Views(FakeApi()).get_nodes()

    assert not (kofin_root(views_env) / "kofintvshowslib2").exists()
    assert (kofin_root(views_env) / "kofinmovieslib1").is_dir()


def test_get_nodes_removes_the_tree_when_nothing_is_synced(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])
    Views(FakeApi()).get_nodes()
    assert kofin_root(views_env).is_dir()

    hand_made = video_root(views_env) / "movies"
    hand_made.mkdir()
    (hand_made / "syncplay.xml").write_text("<node/>")

    sync = sync_db.get_sync()
    sync["Whitelist"] = []
    sync_db.save_sync(sync)
    Views(FakeApi()).get_nodes()

    # The parent goes with the last library, favourites and playlists included.
    assert not kofin_root(views_env).exists()
    assert not playlist_root(views_env).exists()
    assert (hand_made / "syncplay.xml").is_file()


def test_get_nodes_skips_when_hash_unchanged(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])

    views = Views(FakeApi())
    views.get_nodes()

    marker = kofin_root(views_env) / "kofinmovieslib1" / "all.xml"
    marker.unlink()

    # Same state -> generation skipped entirely, file not recreated.
    Views(FakeApi()).get_nodes()
    assert not marker.exists()

    # Whitelist change -> hash differs -> regenerated.
    sync = sync_db.get_sync()
    sync["Whitelist"] = []
    sync_db.save_sync(sync)
    Views(FakeApi()).get_nodes()
    sync["Whitelist"] = ["lib1"]
    sync_db.save_sync(sync)
    Views(FakeApi()).get_nodes()
    assert marker.exists()


def test_remove_library_resets_hash_and_deletes_files(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])
    Views(FakeApi()).get_nodes()
    assert FakeAddon.store["viewsHash"]

    Views().remove_library("lib1")

    assert FakeAddon.store["viewsHash"] == ""
    node_dir = kofin_root(views_env) / "kofinmovieslib1"
    assert not node_dir.exists()
    playlist = playlist_root(views_env) / "kofinmovieslib1.xsp"
    assert not playlist.exists()
    with sync_db.Database("kofin") as opened:
        assert kofindb.JellyfinDatabase(opened.cursor).get_view("lib1") is None


def test_node_icon_mapping():
    assert node_icon("movies") == "DefaultMovies.png"
    assert node_icon("movies", "sets") == "DefaultSets.png"
    assert node_icon("movies", "recent") == "DefaultRecentlyAddedMovies.png"
    assert node_icon("tvshows", "recent") == "DefaultRecentlyAddedEpisodes.png"
    assert node_icon("tvshows", "genres") == "DefaultGenre.png"


def test_cleanonupdate_detection(views_env, monkeypatch):
    profile = views_env["profile"]
    assert kodisetup.cleanonupdate_enabled() is False

    (profile / "advancedsettings.xml").write_text(
        "<advancedsettings><videolibrary>"
        "<cleanonupdate>true</cleanonupdate>"
        "</videolibrary></advancedsettings>"
    )
    assert kodisetup.cleanonupdate_enabled() is True

    # Detection only: the file is never rewritten.
    before = (profile / "advancedsettings.xml").read_text()
    notified = []
    monkeypatch.setattr(
        "kofin.sync.kodisetup.notification", lambda *a, **k: notified.append(a)
    )
    assert kodisetup.warn_incompatible_settings() is True
    assert (profile / "advancedsettings.xml").read_text() == before
    assert notified


# --- the managed playlist folder ---------------------------------------------


def test_generated_playlists_live_in_the_managed_folder(views_env):
    """They used to sit loose among the user's own, where nothing said whose
    they were and no icon could be attached to them."""
    seed([("lib1", "Movies", "movies"), ("lib2", "Shows", "tvshows")], ["lib1", "lib2"])

    Views(FakeApi()).get_nodes()

    assert sorted(p.name for p in playlist_root(views_env).glob("*.xsp")) == [
        "kofinmovieslib1.xsp",
        "kofintvshowslib2.xsp",
    ]
    # Nothing of ours loose in the user's directory.
    assert not list(playlists_root(views_env).glob("*.xsp"))


def test_playlist_folder_carries_the_addon_icon(views_env, monkeypatch):
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    monkeypatch.setattr("kofin.core.settings.addon_path", lambda: repo)
    seed([("lib1", "Movies", "movies")], ["lib1"])

    Views(FakeApi()).get_nodes()

    icon = playlist_root(views_env) / "folder.jpg"
    assert icon.is_file()
    assert icon.read_bytes()[:4] == b"\x89PNG"  # named .jpg, PNG inside


def test_flat_playlists_are_migrated_into_the_folder(views_env):
    """The old copies would otherwise stay: same tag rule, second name, two
    identical entries in the playlists window."""
    seed([("lib1", "Movies", "movies")], ["lib1"])
    (playlists_root(views_env) / "kofinmovieslib1.xsp").write_text("<smartplaylist/>")
    (playlists_root(views_env) / "kofintvshowsgone.xsp").write_text("<smartplaylist/>")
    (playlists_root(views_env) / "mylist.xsp").write_text("<smartplaylist/>")

    Views(FakeApi()).get_nodes()

    assert sorted(p.name for p in playlists_root(views_env).iterdir()) == [
        PLAYLIST_FOLDER,
        "mylist.xsp",  # the user's, and never ours to remove
    ]
    assert (playlist_root(views_env) / "kofinmovieslib1.xsp").is_file()


def test_delete_playlists_spares_what_is_not_ours(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])
    Views(FakeApi()).get_nodes()
    (playlist_root(views_env) / "mine.xsp").write_text("<smartplaylist/>")

    sync = sync_db.get_sync()
    sync["Whitelist"] = []
    sync_db.save_sync(sync)
    Views(FakeApi()).get_nodes()

    # The folder stays for the file that is not ours; ours are gone from it.
    assert not list(playlist_root(views_env).glob("kofin*.xsp"))
    assert (playlist_root(views_env) / "mine.xsp").is_file()


def test_remove_library_finds_a_playlist_in_either_home(views_env):
    """A library dropped between the upgrade and the next generation still has
    its playlist out in the old flat layout."""
    seed([("lib1", "Movies", "movies")], ["lib1"])
    (playlists_root(views_env) / "kofinmovieslib1.xsp").write_text("<smartplaylist/>")

    Views().remove_library("lib1")

    assert not (playlists_root(views_env) / "kofinmovieslib1.xsp").exists()


# --- node ordering resilience (healing-loops-plan F5) ------------------------


def test_get_nodes_survives_a_view_missing_from_sorted_views(views_env):
    """A whitelisted view absent from SortedViews (the /Library/MediaFolders
    403 degradation, or a view that left /UserViews while whitelisted) used
    to ValueError out of node_index before the viewsHash stamp -- a full
    tree rewrite and a traceback on every startup and library command,
    forever. It now orders the stray after everything the server named and
    the hash stamps."""
    from kofin.core import settings

    seed(
        [("lib1", "Movies", "movies"), ("lib2", "Shows", "tvshows")],
        ["lib1", "lib2"],
    )
    sync = sync_db.get_sync()
    sync["SortedViews"] = ["lib1"]  # lib2 fell out of the ordering answer
    sync_db.save_sync(sync)

    Views(FakeApi()).get_nodes()

    index_files = sorted(
        str(p.relative_to(kofin_root(views_env)))
        for p in kofin_root(views_env).rglob("index.xml")
    )
    assert any("lib2" in p for p in index_files)
    assert settings.get_str("viewsHash") != ""

    # The stray sits after everything the server named.
    import xml.etree.ElementTree as etree

    lib2_index = next(
        p for p in kofin_root(views_env).rglob("index.xml") if "lib2" in str(p.parent)
    )
    order = int(etree.parse(str(lib2_index)).getroot().get("order"))
    assert order >= 1  # len(SortedViews) == 1


def test_stray_views_get_stable_distinct_orders(views_env):
    """Two strays must not share an order value or flap between runs: the
    offset comes from the sorted whitelist, which is stable however
    sync.json's set-ordered Whitelist serializes."""
    seed(
        [
            ("lib1", "Movies", "movies"),
            ("lib2", "Shows", "tvshows"),
            ("lib3", "Tunes", "music"),
        ],
        ["lib1", "lib2", "lib3"],
    )
    sync = sync_db.get_sync()
    sync["SortedViews"] = ["lib1"]
    sync_db.save_sync(sync)

    views = Views(FakeApi())

    def within(view_id, media):
        # (rank, media, within, name) -- the stray offset is the third
        return views.node_order({"Id": view_id, "Name": "x", "Media": media})[2]

    first = (within("lib2", "tvshows"), within("lib3", "music"))
    second = (within("lib2", "tvshows"), within("lib3", "music"))

    assert first == second
    assert first[0] != first[1]
    assert min(first) >= 1


def _orders(views_env):
    """Every generated node's order, by label, as Kodi will sort them."""
    import xml.etree.ElementTree as etree

    found = {}
    root = kofin_root(views_env)
    for path in root.rglob("index.xml"):
        if path.parent == root:
            continue  # the Kofin folder node itself
        xml = etree.parse(str(path)).getroot()
        found[xml.find("label").text] = int(xml.get("order"))
    for path in root.glob("kofin_*.xml"):
        # Keyed by file name: the favourites' labels are localized strings,
        # which the Kodi fakes render as "string-<id>".
        xml = etree.parse(str(path)).getroot()
        found[path.stem.replace("kofin_", "")] = int(xml.get("order"))
    return found


def test_libraries_of_one_kind_sit_together(views_env):
    """Reported: two show libraries with a favourites node between them. The
    server's view order interleaves kinds freely and Kodi renders what it is
    handed, so the grouping has to be ours."""
    seed(
        [
            ("lib1", "Movies", "movies"),
            ("lib2", "Shows", "tvshows"),
            ("lib3", "Films", "movies"),
            ("lib4", "Documentaries", "tvshows"),
        ],
        ["lib1", "lib2", "lib3", "lib4"],
    )
    Views(FakeApi()).get_nodes()

    orders = _orders(views_env)
    by_order = [name for name, _ in sorted(orders.items(), key=lambda kv: kv[1])]
    movies = [by_order.index("Movies"), by_order.index("Films")]
    tvshows = [by_order.index("Shows"), by_order.index("Documentaries")]

    assert max(movies) + 1 == min(tvshows), by_order  # no gap, nothing between
    assert max(tvshows) < by_order.index("Favoritemovies"), by_order


def test_the_favourites_block_never_lands_among_the_libraries(views_env):
    """The two used to be numbered in different spaces — libraries by their
    position in the *whole* server view list, favourites by a count of the
    *whitelisted* ones — so a favourite could share an order with a library
    and sort among them."""
    seed(
        [
            ("lib1", "Movies", "movies"),
            ("skipped", "Not synced", "movies"),
            ("also", "Nor this", "tvshows"),
            ("lib2", "Shows", "tvshows"),
        ],
        ["lib1", "lib2"],
    )
    Views(FakeApi()).get_nodes()

    orders = _orders(views_env)
    libraries = [orders["Movies"], orders["Shows"]]
    favourites = [v for k, v in orders.items() if k.startswith("Favorite")]

    assert max(libraries) < min(favourites), orders
    assert len(set(orders.values())) == len(orders), orders  # no collisions


def test_a_mixed_library_splits_to_join_its_own_kinds(views_env):
    """A mixed library is two entries and sorts as two; its halves used to
    travel together in the middle of everything else."""
    seed(
        [("lib1", "Movies", "movies"), ("lib2", "Recordings", "mixed")],
        ["lib1", "lib2"],
    )
    Views(FakeApi()).get_nodes()

    orders = _orders(views_env)
    by_order = [name for name, _ in sorted(orders.items(), key=lambda kv: kv[1])]

    assert by_order.index("Movies") < by_order.index("Recordings (movies)")
    assert by_order.index("Recordings (movies)") < by_order.index(
        "Recordings (tvshows)"
    )
    assert by_order.index("Recordings (tvshows)") < by_order.index("Favoritemovies")


def test_recently_added_albums_asks_for_an_icon_kodi_actually_has():
    """DefaultRecentlyAddedAlbums.png reads like the video names beside it and
    renders as nothing. Kodi's own node for this
    (system/library/music/recentlyaddedalbums.xml) names this one."""
    from kofin.plugin.browse import node_icon as browse_icon

    assert browse_icon("music", "recentalbums") == "DefaultMusicRecentlyAdded.png"


def test_a_music_library_does_not_leave_a_hole_in_the_numbering(views_env):
    """It is whitelisted and sorted like the rest but writes no video node,
    so numbering it anyway pushed the favourites out past a gap."""
    seed(
        [("lib1", "Movies", "movies"), ("lib2", "Tunes", "music")],
        ["lib1", "lib2"],
    )
    Views(FakeApi()).get_nodes()

    orders = sorted(_orders(views_env).values())
    assert orders == list(range(len(orders))), orders


# --- a degraded library listing must not read as "these libraries are gone" --


# Real ids, read off a live 10.11 server: /Library/MediaFolders and /UserViews
# report *different ids for the same Playlists library*, and MediaFolders
# carries one (Music-Alt) the admin's own UserViews does not.
ADMIN_MEDIA_FOLDERS = [
    {
        "Id": "f137a2dd21bbc1b99aa5c0f6bf02a805",
        "Name": "Movies",
        "Type": "CollectionFolder",
        "CollectionType": "movies",
    },
    {
        "Id": "455b9a6cc37d4d2e961d7d5236820ee4",
        "Name": "Music-Alt",
        "Type": "CollectionFolder",
        "CollectionType": "music",
    },
    {
        "Id": "1071671e7bffa0532e930debee501d2e",
        "Name": "Playlists",
        "Type": "CollectionFolder",
        "CollectionType": "playlists",
    },
]
USER_VIEWS_ONLY = [
    {
        "Id": "f137a2dd21bbc1b99aa5c0f6bf02a805",
        "Name": "Movies",
        "Type": "CollectionFolder",
        "CollectionType": "movies",
    },
    {
        "Id": "ee9833e373bf7856254ffbdefa5d641e",
        "Name": "Playlists",
        "Type": "UserView",
        "CollectionType": "playlists",
    },
]


class TwoEndpointApi(FakeApi):
    """A server whose /Library/MediaFolders can be made to fail, as it does
    for a non-admin (403) or on any timeout."""

    def __init__(self, folders, user_views, folders_fail=False):
        self.folders = folders
        self.user_views = user_views
        self.folders_fail = folders_fail

    def media_folders(self):
        if self.folders_fail:
            raise Exception("GET /Library/MediaFolders -> 403")
        return {"Items": self.folders}

    def views(self):
        return {"Items": self.user_views}


def test_a_degraded_listing_does_not_fire_library_removals(views_env, monkeypatch):
    """A 403 or a timeout on /Library/MediaFolders drops get_libraries to
    /UserViews alone. That answer legitimately lacks libraries the healthy one
    had, and gives a *different id* for the same Playlists library — so every
    view sourced from the richer endpoint reads as deleted. The removal it
    fires is not a listing tweak: remove_library deletes every synced row for
    that library out of Kodi's database.
    """
    from kofin.core import ipc

    seed(
        [
            ("f137a2dd21bbc1b99aa5c0f6bf02a805", "Movies", "movies"),
            ("455b9a6cc37d4d2e961d7d5236820ee4", "Music-Alt", "music"),
            ("1071671e7bffa0532e930debee501d2e", "Playlists", "playlists"),
        ],
        ["f137a2dd21bbc1b99aa5c0f6bf02a805"],
    )

    sent = []
    monkeypatch.setattr(ipc, "notify", lambda method, data=None: sent.append(method))

    api = TwoEndpointApi(ADMIN_MEDIA_FOLDERS, USER_VIEWS_ONLY, folders_fail=True)
    Views(api).get_views()

    assert ipc.REMOVE_LIBRARY not in sent, (
        "a transient 403 asked for a library removal: %s" % sent
    )


def test_a_library_the_server_really_dropped_is_still_removed(views_env, monkeypatch):
    """The guard above must not cost the real case: when the listing is
    healthy and a library is genuinely gone, the removal still fires."""
    from kofin.core import ipc

    seed(
        [
            ("f137a2dd21bbc1b99aa5c0f6bf02a805", "Movies", "movies"),
            ("455b9a6cc37d4d2e961d7d5236820ee4", "Music-Alt", "music"),
        ],
        ["f137a2dd21bbc1b99aa5c0f6bf02a805"],
    )

    sent = []
    monkeypatch.setattr(ipc, "notify", lambda method, data=None: sent.append(method))

    # Healthy: MediaFolders answers, and it no longer carries Music-Alt.
    api = TwoEndpointApi([ADMIN_MEDIA_FOLDERS[0]], [], folders_fail=False)
    Views(api).get_views()

    assert ipc.REMOVE_LIBRARY in sent


# --- downloads nodes (offline-downloads plan W1.9) ---------------------------


def test_downloads_nodes_appear_only_while_the_feature_is_on(views_env):
    from kofin.downloads import TAG

    seed([("lib1", "Movies", "movies")], ["lib1"])

    Views(FakeApi()).get_nodes()
    assert not (kofin_root(views_env) / "kofin_DownloadedMovies.xml").exists()

    FakeAddon.store["downloadsEnabled"] = "true"
    Views(FakeApi()).get_nodes()

    movies_node = kofin_root(views_env) / "kofin_DownloadedMovies.xml"
    shows_node = kofin_root(views_env) / "kofin_DownloadedShows.xml"
    assert movies_node.is_file() and shows_node.is_file()
    movies_xml = movies_node.read_text()
    assert "<value>%s</value>" % TAG in movies_xml  # the tag rule
    assert "<content>movies</content>" in movies_xml
    assert "<content>tvshows</content>" in shows_node.read_text()
    # The label is written out as text: a bare 30xxx resolves against Kodi's
    # own strings, where the addon range is empty (CGUIControlFactory).
    assert "<label>3071" not in movies_xml


def test_downloaded_episodes_node_filters_on_path_not_tag(views_env):
    """Kodi compiles a tag rule on an episodes node against the *show*
    (SmartPlayList.cpp), so it answers with every episode of every tagged
    show — 25+ rows for two downloads, measured live. The file's location is
    the honest signal, and the repoint already put it under the root."""
    seed([("lib1", "Movies", "movies")], ["lib1"])
    FakeAddon.store["downloadsEnabled"] = "true"

    Views(FakeApi()).get_nodes()

    node = kofin_root(views_env) / "kofin_DownloadedEpisodes.xml"
    assert node.is_file()
    xml = node.read_text()
    assert "<content>episodes</content>" in xml
    assert 'field="path" operator="startswith"' in xml
    assert 'field="tag"' not in xml
    assert xml.rstrip().endswith("</node>")
    # The trailing separator keeps startswith off a sibling that merely
    # shares the prefix ("downloads-old/").
    from kofin.downloads import downloads_root

    assert "<value>%s/</value>" % downloads_root().rstrip("/") in xml


def test_moving_the_downloads_root_regenerates_the_tree(views_env):
    """The rule embeds the root, so the path is part of the tree's identity."""
    seed([("lib1", "Movies", "movies")], ["lib1"])
    FakeAddon.store["downloadsEnabled"] = "true"
    FakeAddon.store["downloadsPath"] = "/old/downloads/"
    Views(FakeApi()).get_nodes()
    first = (kofin_root(views_env) / "kofin_DownloadedEpisodes.xml").read_text()

    FakeAddon.store["downloadsPath"] = "/new/downloads/"
    Views(FakeApi()).get_nodes()
    second = (kofin_root(views_env) / "kofin_DownloadedEpisodes.xml").read_text()

    assert "/old/downloads/" in first and "/new/downloads/" in second


def test_toggling_downloads_regenerates_the_tree(views_env):
    """The hash folds the toggle in, so turning the feature off takes the
    nodes away rather than leaving them behind on an unchanged view set."""
    seed([("lib1", "Movies", "movies")], ["lib1"])
    FakeAddon.store["downloadsEnabled"] = "true"
    Views(FakeApi()).get_nodes()
    assert (kofin_root(views_env) / "kofin_DownloadedMovies.xml").is_file()

    FakeAddon.store["downloadsEnabled"] = "false"
    Views(FakeApi()).get_nodes()

    assert not (kofin_root(views_env) / "kofin_DownloadedMovies.xml").exists()
    assert not (kofin_root(views_env) / "kofin_DownloadedShows.xml").exists()


def test_downloaded_nodes_carry_the_addon_icon_even_on_an_existing_file(views_env):
    """The Downloads singles inherited DefaultFavourites.png from the
    favourites code they share — a favourite star on the downloads shelf.

    The rewrite matters as much as the icon: ``add_single_node`` only ever
    wrote ``<icon>`` on the *create* branch, so an install that already had
    these files would have kept the star through any number of NODE_LAYOUT
    bumps.
    """
    from kofin.sync.views import NODE_DOWNLOADS_ICON

    seed([("lib1", "Movies", "movies")], ["lib1"])
    FakeAddon.store["downloadsEnabled"] = "true"
    Views(FakeApi()).get_nodes()

    node = kofin_root(views_env) / "kofin_DownloadedMovies.xml"
    assert "<icon>%s</icon>" % NODE_DOWNLOADS_ICON in node.read_text()
    # The favourites keep Kodi's own stock icon: a skin has something of its
    # own to substitute there.
    favourite = kofin_root(views_env) / "kofin_Favoritemovies.xml"
    assert "DefaultFavourites.png" in favourite.read_text()

    # Now the upgrade path: an existing file carrying the old icon.
    node.write_text(
        node.read_text().replace(NODE_DOWNLOADS_ICON, "DefaultFavourites.png")
    )
    FakeAddon.store["viewsHash"] = ""
    Views(FakeApi()).get_nodes()
    assert "<icon>%s</icon>" % NODE_DOWNLOADS_ICON in node.read_text()


def test_music_nodes_appear_and_leave_with_the_feature(views_env):
    """Kodi keeps music nodes in a tree of their own, so nothing in the
    video generation reaches them: the downloads feature had a
    Downloaded-music smart playlist and no node, which filed it under
    Playlists instead of beside the rest of Kofin."""
    from kofin.sync.views import (
        MUSIC_DOWNLOADED_FILE,
        NODE_DOWNLOADS_ICON,
        NODE_ROOT_ICON,
        music_node_root_path,
        write_music_nodes,
    )

    root = pathlib.Path(music_node_root_path())
    assert write_music_nodes() is False  # feature off: nothing written
    assert not root.exists()

    FakeAddon.store["downloadsEnabled"] = "true"
    assert write_music_nodes() is True

    index = (root / "index.xml").read_text()
    assert "<icon>%s</icon>" % NODE_ROOT_ICON in index

    node = (root / MUSIC_DOWNLOADED_FILE).read_text()
    assert "<content>songs</content>" in node
    assert "<icon>%s</icon>" % NODE_DOWNLOADS_ICON in node
    assert 'field="path" operator="startswith"' in node
    from kofin.downloads import downloads_root

    assert "<value>%s/Music/</value>" % downloads_root().rstrip("/") in node

    FakeAddon.store["downloadsEnabled"] = "false"
    assert write_music_nodes() is False
    assert not (root / MUSIC_DOWNLOADED_FILE).exists()
    assert not root.exists()  # emptied, so the folder goes too


def test_music_node_removal_leaves_a_hand_made_node_alone(views_env):
    """Same rule as the video pruner: this folder is ours by name only, and
    anything else in it is the user's."""
    from kofin.sync.views import music_node_root_path, write_music_nodes

    FakeAddon.store["downloadsEnabled"] = "true"
    write_music_nodes()
    root = pathlib.Path(music_node_root_path())
    (root / "mine.xml").write_text("<node/>")

    FakeAddon.store["downloadsEnabled"] = "false"
    write_music_nodes()

    assert (root / "mine.xml").is_file()
    assert root.is_dir()  # not emptied, so not removed


def test_serverless_node_generation_asks_no_server(views_env, monkeypatch):
    """The settings-apply rebuild (a downloads toggle) constructs Views with
    no server. window_nodes drives its loop from kofin.db's own view table and
    window_artwork clears the tile prop outright when there is no server, so
    the media-folder listing has nothing to contribute on that path — it just
    used to blow up on NoneType and log two tracebacks per toggle."""
    seed(
        [("lib1", "Movies", "movies")],
        ["lib1"],
    )

    def explode(self):
        raise AssertionError("get_libraries must not be called without a server")

    monkeypatch.setattr(Views, "get_libraries", explode)

    Views().get_nodes()


def test_listing_libraries_without_a_server_names_the_caller_error(views_env):
    """The local-only paths are allowed no server; asking this one for the
    server's listing is a caller error and should say so."""
    with pytest.raises(IndexError, match="no server"):
        Views().get_libraries()
