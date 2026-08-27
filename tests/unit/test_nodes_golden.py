"""Golden-file tests for the generated node tree and its skin props (P2.0a).

Everything ``Views.get_nodes()`` writes -- the video node tree, the managed
smart playlists, the music node tree -- and every ``Kofin.nodes.*`` /
``Kofin.wnodes.*`` window property, for two fixtures, compared byte for
byte against ``tests/fixtures/nodes/<case>/``. The tree is a skin-facing
contract and the props are the one skins read, so a change here is a change
to that contract: regenerate with ``KOFIN_UPDATE_GOLDEN=1`` after an
*intended* change and read the diff before committing it.

The temp directory the fixture runs in is masked as ``<tmp>`` so paths the
tree embeds (the downloads root in the Downloaded nodes) stay stable.
"""

import json
import os
import pathlib
import shutil

import pytest

from kofin.sync import db as sync_db
from kofin.sync.views import Views
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.test_sync_views import FakeApi, seed, views_env  # noqa: F401

GOLDEN = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "nodes"

TREES = (
    ("library", "video", "kofin"),
    ("library", "music", "kofin"),
    ("playlists", "video", "Kofin"),
)


def snapshot(views_env):
    """{relative path: text} for every generated file, tmp path masked."""
    profile = views_env["profile"]
    mask = str(profile.parent)
    files = {}
    for parts in TREES:
        top = profile.joinpath(*parts)
        if not top.is_dir():
            continue
        for path in sorted(top.rglob("*")):
            if path.is_file():
                text = path.read_bytes().decode("utf-8", errors="replace")
                files[str(path.relative_to(profile))] = text.replace(mask, "<tmp>")
    return files


def props():
    return {
        name: value
        for name, value in sorted(FakeWindow.store.items())
        if name.startswith("Kofin.")
    }


def check(case, files, window):
    folder = GOLDEN / case
    if os.environ.get("KOFIN_UPDATE_GOLDEN"):
        shutil.rmtree(folder, ignore_errors=True)
        for relative, text in files.items():
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        (folder / "props.json").write_text(json.dumps(window, indent=1, sort_keys=True))

    expected_props = json.loads((folder / "props.json").read_text())
    expected = {
        str(path.relative_to(folder)): path.read_text()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != "props.json"
    }
    assert files == expected
    assert window == expected_props


VIDEO_VIEWS = [
    ("lib2", "Shows", "tvshows"),
    ("lib1", "Movies", "movies"),
    ("lib3", "Both", "mixed"),
    ("lib5", "Clips", "musicvideos"),
    ("lib4", "Tunes", "music"),
    ("lib6", "Home movies", "homevideos"),  # dynamic entry: props only
]
VIDEO_WHITELIST = ["lib1", "lib2", "Mixed:lib3", "lib4", "lib5"]

MUSIC_VIEWS = [
    ("libm1", "Tunes", "music"),
    ("libm2", "Tunes", "music"),  # the same name twice: source disambiguation
    ("lib1", "Movies", "movies"),
]


def test_video_tree_matches_golden(views_env):
    seed(VIDEO_VIEWS, VIDEO_WHITELIST)

    Views(FakeApi()).get_nodes()

    check("video", snapshot(views_env), props())


def test_music_and_downloads_tree_matches_golden(views_env):
    seed(MUSIC_VIEWS, ["libm1", "libm2", "lib1"])
    FakeAddon.store["downloadsEnabled"] = "true"
    FakeAddon.store["downloadsPath"] = "special://profile/dl"

    Views(FakeApi()).get_nodes()

    check("music-downloads", snapshot(views_env), props())


def test_a_second_generation_is_byte_identical(views_env):
    """The idempotency half: regenerating over an existing tree changes no
    file and no prop."""
    seed(VIDEO_VIEWS, VIDEO_WHITELIST)
    Views(FakeApi()).get_nodes()
    first, first_props = snapshot(views_env), props()

    FakeAddon.store["viewsHash"] = ""  # force the file pass to run again
    Views(FakeApi()).get_nodes()

    assert snapshot(views_env) == first
    assert props() == first_props


def rename_view(view_id, name):
    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE view SET view_name = ? WHERE view_id = ?", (name, view_id)
        )


def tag_rules(text):
    import xml.etree.ElementTree as etree

    root = etree.fromstring(text)
    return [
        rule.find("value").text
        for rule in root.findall("rule")
        if rule.get("field") == "tag"
    ]


def test_a_renamed_library_carries_only_its_new_tag(views_env):
    """Failed before P2.1: node files were parsed and amended, so a renamed
    library kept its old tag rule beside the new one and matched nothing
    under match=all. Written whole, the file says what the server says."""
    seed(VIDEO_VIEWS, VIDEO_WHITELIST)
    Views(FakeApi()).get_nodes()
    root = views_env["profile"] / "library" / "video" / "kofin"
    assert tag_rules((root / "kofinmovieslib1" / "all.xml").read_text()) == ["Movies"]

    rename_view("lib1", "Films")
    Views(FakeApi()).get_nodes()  # the name is in the hash: regenerates

    assert tag_rules((root / "kofinmovieslib1" / "all.xml").read_text()) == ["Films"]
    playlist = views_env["profile"] / "playlists" / "video" / "Kofin"
    assert tag_rules((playlist / "kofinmovieslib1.xsp").read_text()) == ["Films"]


# --- the props are cleared as completely as they are published (P2.1c) ---------


def test_republishing_a_smaller_view_set_leaves_no_stale_props(views_env):
    """A library removed from the whitelist: every Kofin.nodes.N.* prop it
    published is cleared on the next publish, the sub-node ones included --
    the fork's fixed clear list missed genres/random/recommended/sets and
    every sub-node's id/type/artwork."""
    seed(VIDEO_VIEWS, VIDEO_WHITELIST)
    Views(FakeApi()).get_nodes()
    before = props()
    assert any(name.startswith("Kofin.nodes.1.genres.") for name in before)
    assert any(name.startswith("Kofin.nodes.1.recommended.id") for name in before)

    for view_id, _name, _media in VIDEO_VIEWS[1:]:
        Views().remove_library(view_id)  # only Shows stays
    seed(VIDEO_VIEWS[:1], ["lib2"])
    Views(FakeApi()).get_nodes()
    after = props()

    removed = [view_id for view_id, _name, _media in VIDEO_VIEWS[1:]]
    stale = sorted(
        name
        for name, value in after.items()
        if any(view_id in value for view_id in removed)
    )
    assert stale == []
    assert after["Kofin.nodes.total"] == "4"  # Shows + the three favourites
    assert after["Kofin.nodes.0.id"] == "lib2"
    # The singles at 1..3 publish entry props only; a sub-node prop there
    # could only be a leftover.
    assert not any(
        name.startswith("Kofin.nodes.%d." % index) and name.count(".") > 3
        for index in (1, 2, 3)
        for name in after
    )
    assert not any(name.startswith("Kofin.wnodes.1.") for name in after)


def test_clear_covers_every_sub_node_the_table_knows(views_env):
    from kofin.sync.nodes import props as node_props
    from kofin.sync.nodes.video import NODES

    FakeWindow.store["Kofin.nodes.total"] = "1"
    planted = []
    for kind in NODES.values():
        for key, _label in kind:
            for prop in ("title", "content", "path", "id", "type", "artwork"):
                name = "Kofin.nodes.0.%s.%s" % (key, prop)
                FakeWindow.store[name] = "x"
                planted.append(name)
    FakeWindow.store["Kofin.nodes.0.title"] = "x"
    FakeWindow.store["Kofin.nodes.title"] = "x"

    node_props.clear()

    assert not any(name in FakeWindow.store for name in planted if ".all." not in name)
    assert "Kofin.nodes.0.title" not in FakeWindow.store
    assert "Kofin.nodes.title" not in FakeWindow.store


def test_a_whitelisted_kind_without_nodes_is_skipped_not_fatal(views_env):
    """Live, 2026-08-27: a boxsets view reached the whitelist (update mode
    whitelists whatever it planned), and NODES["boxsets"] raised at
    startup -- the library thread died with it. The tree is written
    without the entry and the rest stands."""
    seed(
        [("lib1", "Movies", "movies"), ("libc", "Collections", "boxsets")],
        ["lib1", "libc"],
    )

    Views(FakeApi()).get_nodes()

    root = views_env["profile"] / "library" / "video" / "kofin"
    assert (root / "kofinmovieslib1" / "all.xml").is_file()
    assert not any(path.name.endswith("libc") for path in root.iterdir())
    assert props()["Kofin.nodes.0.id"] == "lib1"
