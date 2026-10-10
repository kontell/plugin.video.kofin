"""The API build's node tree and skin entries (sync/dynamic.py)."""

import xml.etree.ElementTree as ET

from kofin.sync import dynamic, private
from kofin.sync.nodes.video import NODE_ROOT
from tests.unit.fakes import FakeWindow
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
