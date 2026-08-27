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


@pytest.mark.xfail(
    strict=True,
    reason="P2.1: node files are parsed and amended, so a renamed library keeps "
    "its old tag rule beside the new one and matches nothing under match=all",
)
def test_a_renamed_library_carries_only_its_new_tag(views_env):
    seed(VIDEO_VIEWS, VIDEO_WHITELIST)
    Views(FakeApi()).get_nodes()
    root = views_env["profile"] / "library" / "video" / "kofin"
    assert tag_rules((root / "kofinmovieslib1" / "all.xml").read_text()) == ["Movies"]

    rename_view("lib1", "Films")
    Views(FakeApi()).get_nodes()  # the name is in the hash: regenerates

    assert tag_rules((root / "kofinmovieslib1" / "all.xml").read_text()) == ["Films"]
    playlist = views_env["profile"] / "playlists" / "video" / "Kofin"
    assert tag_rules((playlist / "kofinmovieslib1.xsp").read_text()) == ["Films"]
