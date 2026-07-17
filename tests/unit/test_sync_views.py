"""Views/nodes generation: file shapes, stock icons, and the views-hash
regeneration guard (plan §5 step 4)."""

import os

import pytest

from kofin.sync import db as sync_db
from kofin.sync import kofindb
from kofin.sync import kodisetup
from kofin.sync.views import Views, node_icon
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


def test_get_nodes_generates_files_with_stock_icons(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])

    Views(FakeApi()).get_nodes()

    node_dir = views_env["profile"] / "library" / "video" / "kofinmovieslib1"
    assert (node_dir / "index.xml").is_file()
    assert (node_dir / "all.xml").is_file()
    assert (node_dir / "recent.xml").is_file()

    index_xml = (node_dir / "index.xml").read_text()
    assert "<icon>DefaultMovies.png</icon>" in index_xml
    assert "icon.png" not in index_xml  # never addon art on structural entries

    recent_xml = (node_dir / "recent.xml").read_text()
    assert "<icon>DefaultRecentlyAddedMovies.png</icon>" in recent_xml
    assert "Movies" in recent_xml  # tag rule on the library name

    playlist = views_env["profile"] / "playlists" / "video" / "kofinmovieslib1.xsp"
    assert playlist.is_file()
    assert "<value>Movies</value>" in playlist.read_text()

    # Window props got populated.
    assert FakeWindow.store.get("Kofin.nodes.total")


def test_get_nodes_skips_when_hash_unchanged(views_env):
    seed([("lib1", "Movies", "movies")], ["lib1"])

    views = Views(FakeApi())
    views.get_nodes()

    marker = views_env["profile"] / "library" / "video" / "kofinmovieslib1" / "all.xml"
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
    node_dir = views_env["profile"] / "library" / "video" / "kofinmovieslib1"
    assert not node_dir.exists()
    playlist = views_env["profile"] / "playlists" / "video" / "kofinmovieslib1.xsp"
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
