"""The API build's node tree and skin entries (sync/dynamic.py)."""

import os
import xml.etree.ElementTree as ET

import pytest

from kofin.sync import dynamic, private
from kofin.sync.backends.api import capabilities
from kofin.sync.nodes.video import NODE_ROOT
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
    """A profile directory behind every special:// path the node writers
    touch, and kofin.db beside it."""
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    # A Kodi 23 with the changes unless a test narrows it.
    monkeypatch.setattr(capabilities, "features", lambda: capabilities.ALL)

    profile = tmp_path / "profile"
    (profile / "library" / "video").mkdir(parents=True)
    (profile / "playlists" / "video").mkdir(parents=True)
    (profile / "addon_data" / "plugin.video.kofin").mkdir(parents=True)

    def translate(path):
        if path.startswith("special://profile"):
            rest = path.replace("special://profile", "").strip("/")
            return str(profile / rest) if rest else str(profile)
        return str(tmp_path / path.replace("special://", "").strip("/"))

    def translate_or_raw(path):
        return translate(path) if path.startswith("special://") else path

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
    monkeypatch.setattr("xbmcvfs.listdir", fake_listdir)

    private.reset_overrides()
    private.set_path_override(
        "kofin", str(profile / "addon_data" / "plugin.video.kofin" / "kofin.db")
    )
    yield {"profile": profile}
    private.reset_overrides()


MOVIES = "1" * 32
SHOWS = "2" * 32
MUSIC = "3" * 32
MIXED = "4" * 32

ITEMS = [
    {"Id": SHOWS, "Name": "Shows", "CollectionType": "tvshows"},
    {"Id": MOVIES, "Name": "Movies", "CollectionType": "movies"},
    {"Id": MUSIC, "Name": "Music", "CollectionType": "music"},
    {"Id": MIXED, "Name": "Mixed", "CollectionType": None},
    {"Id": "5" * 32, "Name": "Live", "CollectionType": "livetv"},
]


def whitelist(*ids):
    sync = private.get_sync()
    sync["Whitelist"] = list(ids)
    private.save_sync(sync)


def props():
    return {k: v for k, v in sorted(FakeWindow.store.items()) if k.startswith("Kofin.")}


def test_a_whitelisted_library_gets_its_tree_and_library_paths(views_env):
    whitelist(MOVIES, MUSIC)
    dynamic.publish(ITEMS, FakeApi())
    root = views_env["profile"] / "library" / "video" / NODE_ROOT
    folder = root / ("kofinmovies" + MOVIES)
    assert (root / "index.xml").is_file()
    assert (folder / "index.xml").is_file() and (folder / "recent.xml").is_file()
    # The filter nodes select by the tag the API backend puts on every row.
    rule = ET.parse(folder / "all.xml").getroot().find("rule")
    assert rule.get("field") == "tag" and rule.find(
        "value"
    ).text == dynamic.library_tag(MOVIES)
    # Favourites singles ride along; no downloads single in this build.
    # One smart playlist per synced video library, by the same tag.
    xsp = (
        views_env["profile"]
        / "playlists"
        / "video"
        / "Kofin"
        / ("kofinmovies%s.xsp" % MOVIES)
    )
    assert xsp.is_file() and dynamic.library_tag(MOVIES) in xsp.read_text()
    singles = sorted(p.name for p in root.glob("kofin_*.xml"))
    assert singles == [
        "kofin_Favoriteepisodes.xml",
        "kofin_Favoritemovies.xml",
        "kofin_Favoritetvshows.xml",
    ]
    window = props()
    # Entries follow the server's order: Shows (dynamic), Movies (native),
    # Music (native music root), Mixed (dynamic); live TV is left out.
    assert window["Kofin.nodes.0.title"] == "Shows"
    assert window["Kofin.nodes.0.path"].startswith("plugin://plugin.video.kofin/?")
    assert window["Kofin.nodes.1.content"] == (
        "library://video/%s/kofinmovies%s/all.xml" % (NODE_ROOT, MOVIES)
    )
    assert window["Kofin.nodes.1.recent.content"].endswith("/recent.xml")
    assert (
        window["Kofin.nodes.2.path"] == "ActivateWindow(Music,library://music/,return)"
    )
    assert window["Kofin.nodes.3.title"] == "Mixed"
    assert window["Kofin.nodes.3.path"].startswith("plugin://plugin.video.kofin/?")
    assert "5" * 32 not in " ".join(window.values())


def test_favourite_episodes_are_found_by_their_own_tag(views_env):
    """Kodi 23 keeps an episode's tags and its tag rule matches them
    (kontell/xbmc #12); a favourite episode carries the tag."""
    whitelist(MOVIES)
    dynamic.publish(ITEMS, FakeApi())
    root = views_env["profile"] / "library" / "video" / NODE_ROOT
    found = ET.parse(root / "kofin_Favoriteepisodes.xml").getroot().find("rule")
    assert (found.get("field"), found.find("value").text) == (
        "tag",
        "Favorite episodes",
    )


def test_downloaded_episodes_are_found_by_a_tag_their_show_does_not_carry(
    views_env, monkeypatch
):
    """A tag rule on episodes matches the episode's tags or its show's
    (kontell/xbmc #12), so the node filters on the episode's own download
    tag, and a Kodi without episode tags has no such node."""
    from kofin.downloads import EPISODE_TAG, TAG

    FakeAddon.store["downloadsEnabled"] = "true"
    whitelist(MOVIES)
    dynamic.publish(ITEMS, FakeApi())
    root = views_env["profile"] / "library" / "video" / NODE_ROOT
    node = ET.parse(root / "kofin_DownloadedEpisodes.xml").getroot()
    rule = node.find("rule")
    assert node.find("content").text == "episodes"
    assert (rule.get("field"), rule.find("value").text) == ("tag", EPISODE_TAG)
    assert EPISODE_TAG != TAG
    shows = ET.parse(root / "kofin_DownloadedShows.xml").getroot().find("rule")
    assert shows.find("value").text == TAG
    monkeypatch.setattr(capabilities, "features", lambda: frozenset())
    dynamic.publish(ITEMS, FakeApi())
    assert not (root / "kofin_DownloadedEpisodes.xml").exists()
    assert (root / "kofin_DownloadedShows.xml").is_file()


def test_a_library_leaving_the_whitelist_loses_its_folder_and_an_empty_one_the_tree(
    views_env,
):
    whitelist(MOVIES, SHOWS, MIXED)
    dynamic.publish(ITEMS, FakeApi())
    root = views_env["profile"] / "library" / "video" / NODE_ROOT
    folders = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert folders == sorted(
        [
            "kofinmovies" + MOVIES,
            "kofintvshows" + SHOWS,
            "kofinmovies" + MIXED,
            "kofintvshows" + MIXED,
        ]
    )
    # A hand-made node beside ours is the user's.
    (root / "mine.xml").write_text("<node/>")
    whitelist(SHOWS)
    dynamic.publish(ITEMS, FakeApi())
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == [
        "kofintvshows" + SHOWS
    ]
    assert props()["Kofin.nodes.1.path"].startswith("plugin://")
    whitelist()
    dynamic.publish(ITEMS, FakeApi())
    assert [p.name for p in root.iterdir()] == ["mine.xml"]
    assert props()["Kofin.nodes.0.path"].startswith("plugin://")


def test_tree_order_is_movies_then_shows_within_the_servers_order(views_env):
    whitelist(SHOWS, MOVIES, MIXED)
    entries = dynamic.node_entries(
        [
            dynamic.View(i["Id"], i["Name"], i.get("CollectionType") or "mixed")
            for i in ITEMS
        ],
        [SHOWS, MOVIES, MIXED],
    )
    assert [(v["Media"], v["Id"][:1], mixed) for v, mixed in entries] == [
        ("movies", "1", False),
        ("movies", "4", True),
        ("tvshows", "2", False),
        ("tvshows", "4", True),
    ]


def test_the_switch_takes_the_tree_and_playlists_down_and_publishes_browse_entries(
    views_env,
):
    """libraryNodes off: the files Kofin wrote go, a hand-made node stays, a
    synced video library is a browse entry and a synced music library keeps
    Kodi's music root; on again, the tree and the playlists come back."""
    whitelist(MOVIES, MUSIC)
    dynamic.publish(ITEMS, FakeApi())
    root = views_env["profile"] / "library" / "video" / NODE_ROOT
    xsp = (
        views_env["profile"]
        / "playlists"
        / "video"
        / "Kofin"
        / ("kofinmovies%s.xsp" % MOVIES)
    )
    assert (root / ("kofinmovies" + MOVIES) / "all.xml").is_file() and xsp.is_file()
    (root / "mine.xml").write_text("<node/>")
    FakeAddon.store[dynamic.SETTING] = "false"
    assert dynamic.wanted() is False
    dynamic.publish(ITEMS, FakeApi())
    assert [p.name for p in root.iterdir()] == ["mine.xml"]
    assert not xsp.exists()
    window = props()
    assert window["Kofin.nodes.1.title"] == "Movies"
    assert window["Kofin.nodes.1.path"].startswith("plugin://plugin.video.kofin/?")
    assert "Kofin.nodes.1.content" not in window
    assert (
        window["Kofin.nodes.2.path"] == "ActivateWindow(Music,library://music/,return)"
    )
    FakeAddon.store[dynamic.SETTING] = "true"
    dynamic.publish(ITEMS, FakeApi())
    assert (root / ("kofinmovies" + MOVIES) / "all.xml").is_file() and xsp.is_file()
    assert (root / "mine.xml").is_file()
    assert props()["Kofin.nodes.1.content"].endswith("/all.xml")


def test_only_an_explicit_off_switches_the_presentation_off(monkeypatch):
    """Off deletes files: an empty read (a settings document that did not
    load) and a build without the setting both read as on."""
    from kofin.core import settings

    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    assert dynamic.wanted() is True
    FakeAddon.store[dynamic.SETTING] = "false"
    assert dynamic.wanted() is False
    FakeAddon.store[dynamic.SETTING] = "true"
    assert dynamic.wanted() is True

    def missing(_setting):
        raise TypeError("no such setting")

    monkeypatch.setattr(settings, "get_str", missing)
    assert dynamic.wanted() is True


def test_the_shipped_video_nodes_are_seeded_before_the_tree_is_written(
    views_env, monkeypatch
):
    """Once the profile has a library/video/ folder, every library://video/
    path resolves there: without Kodi's own movies/ and tvshows/ nodes a
    skin's categories row (library://video/movies/) lists nothing."""
    import os
    import shutil
    import xbmcvfs

    monkeypatch.setattr(
        "xbmcvfs.copy", lambda src, dst: shutil.copyfile(src, dst) or True
    )
    system = xbmcvfs.translatePath("special://xbmc/system/library/video")
    os.makedirs(os.path.join(system, "movies"))
    with open(os.path.join(system, "movies", "index.xml"), "w") as handle:
        handle.write("<node/>")
    with open(os.path.join(system, "movies", "genres.xml"), "w") as handle:
        handle.write("<node/>")
    profile = views_env["profile"] / "library" / "video"
    (profile / "movies").mkdir()
    (profile / "movies" / "genres.xml").write_text("<node><mine/></node>")
    whitelist(MOVIES)
    dynamic.publish(ITEMS, FakeApi())
    assert (profile / "movies" / "index.xml").read_text() == "<node/>"
    # A node the user edited is theirs.
    assert (profile / "movies" / "genres.xml").read_text() == "<node><mine/></node>"
    assert (profile / NODE_ROOT / "index.xml").is_file()


def logged_in():
    FakeAddon.store.update(
        {"serverId": "s" * 32, "userId": "u" * 32, "deviceId": "d" * 32}
    )


def test_a_whitelisted_music_library_gets_its_folder_in_the_music_tree(
    views_env, monkeypatch
):
    """The Manage menu reads a library's downloads from its folder in the
    music tree. The folder's nodes take the library's songs by the folder they
    are filed under, Kodi's own music nodes are seeded first, and there is no
    Downloaded folder, whose rule on the downloads folder matches nothing in
    this build."""
    import os
    import shutil

    import xbmcvfs

    from kofin.sync.backends.api import paths
    from kofin.sync.backends.api.store import namespace

    monkeypatch.setattr(
        "xbmcvfs.copy", lambda src, dst: shutil.copyfile(src, dst) or True
    )
    system = xbmcvfs.translatePath("special://xbmc/system/library/music")
    os.makedirs(os.path.join(system, "albums"))
    with open(os.path.join(system, "albums", "index.xml"), "w") as handle:
        handle.write("<node/>")
    logged_in()
    FakeAddon.store["downloadsEnabled"] = "true"
    whitelist(MOVIES, MUSIC)
    dynamic.publish(ITEMS, FakeApi())
    music = views_env["profile"] / "library" / "music"
    folder = music / NODE_ROOT / ("kofinmusic" + MUSIC)
    assert (music / "albums" / "index.xml").read_text() == "<node/>"
    assert sorted(p.name for p in (music / NODE_ROOT).iterdir() if p.is_dir()) == [
        "kofinmusic" + MUSIC
    ]
    assert sorted(p.name for p in folder.iterdir()) == [
        "albums.xml",
        "artists.xml",
        "genres.xml",
        "index.xml",
        "songs.xml",
    ]
    for stem in ("albums", "artists", "genres", "songs"):
        rule = ET.parse(folder / ("%s.xml" % stem)).getroot().find("rule")
        assert (rule.get("field"), rule.get("operator")) == ("path", "startswith")
        assert rule.find("value").text == paths.library_dir(
            namespace("s" * 32, "u" * 32), MUSIC, "music"
        )
    # The skin entry stays Kodi's music root, as in the SQL build.
    assert (
        props()["Kofin.nodes.2.path"] == "ActivateWindow(Music,library://music/,return)"
    )
    # The switch off takes the tree down, and so does leaving the selection.
    FakeAddon.store[dynamic.SETTING] = "false"
    dynamic.publish(ITEMS, FakeApi())
    assert not (music / NODE_ROOT).exists()
    FakeAddon.store[dynamic.SETTING] = "true"
    dynamic.publish(ITEMS, FakeApi())
    assert folder.is_dir()
    whitelist(MOVIES)
    dynamic.publish(ITEMS, FakeApi())
    assert not (music / NODE_ROOT).exists()
    assert (music / "albums" / "index.xml").is_file()


def test_no_music_tree_is_written_before_a_login(views_env):
    whitelist(MUSIC)
    dynamic.publish(ITEMS, FakeApi())
    assert not (views_env["profile"] / "library" / "music" / NODE_ROOT).exists()


def test_the_music_tree_survives_a_settings_read_that_cannot_answer(
    views_env, monkeypatch
):
    """An empty list deletes the music tree, so blank ids delete it only from
    a store that answered: an unregistered add-on and a settings document
    that did not load read blank too, and leave the tree as it is. A logout
    takes it down."""
    from kofin.core import settings

    logged_in()
    whitelist(MUSIC)
    dynamic.publish(ITEMS, FakeApi())
    tree = views_env["profile"] / "library" / "music" / NODE_ROOT
    assert (tree / ("kofinmusic" + MUSIC)).is_dir()
    # Kodi has the add-on unregistered, as during an update.
    get_addon = settings.get_addon
    monkeypatch.setattr(settings, "get_addon", lambda: None)
    dynamic.publish(ITEMS, FakeApi())
    assert (tree / ("kofinmusic" + MUSIC)).is_dir()
    monkeypatch.setattr(settings, "get_addon", get_addon)
    # The settings document did not load: every value blank.
    stored = dict(FakeAddon.store)
    FakeAddon.store = {}
    dynamic.publish(ITEMS, FakeApi())
    assert (tree / ("kofinmusic" + MUSIC)).is_dir()
    # A logout keeps the device id and clears the rest.
    FakeAddon.store = {
        k: v for k, v in stored.items() if k not in ("serverId", "userId")
    }
    dynamic.publish(ITEMS, FakeApi())
    assert not tree.exists()


def test_the_favourite_episodes_node_is_written_only_where_kodi_keeps_episode_tags(
    views_env, monkeypatch
):
    """A tag rule on episodes lists nothing on a Kodi without kontell/xbmc
    #12, so the single is not written there, and one an earlier Kodi wrote
    is pruned with the rewrite."""
    whitelist(MOVIES)
    dynamic.publish(ITEMS, FakeApi())
    root = views_env["profile"] / "library" / "video" / NODE_ROOT
    assert (root / "kofin_Favoriteepisodes.xml").is_file()
    monkeypatch.setattr(capabilities, "features", lambda: frozenset())
    dynamic.publish(ITEMS, FakeApi())
    assert not (root / "kofin_Favoriteepisodes.xml").exists()
    assert (root / "kofin_Favoritemovies.xml").is_file()
    assert "Favorite episodes" not in " ".join(props().values())
