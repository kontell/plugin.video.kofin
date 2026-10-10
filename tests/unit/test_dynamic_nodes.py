"""The API build's node tree and skin entries (sync/dynamic.py)."""

import xml.etree.ElementTree as ET

from kofin.sync import dynamic, private
from kofin.sync.nodes.video import NODE_ROOT
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.test_sync_views import FakeApi, views_env  # noqa: F401

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
