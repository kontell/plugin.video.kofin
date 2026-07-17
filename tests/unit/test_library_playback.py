"""L1: context transcode resolves library items through kofin.db when the
kofin.id property is absent (plan §5 step 6)."""

import sys

import pytest

from kofin.plugin import context
from kofin.sync import db as sync_db
from kofin.sync import kofindb
from tests.unit.fakes import FakeAddon, FakeWindow


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda p: str(tmp_path))
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


def seed_reference(item_id, kodi_id, media_type, jellyfin_type):
    with sync_db.Database("kofin") as opened:
        kofindb.JellyfinDatabase(opened.cursor).add_reference(
            item_id, kodi_id, 1, 1, jellyfin_type, media_type, None, "etag", "lib", None
        )


def test_lookup_item_id_resolves_library_rows():
    seed_reference("movie1", 12, "movie", "Movie")
    seed_reference("episode1", 34, "episode", "Episode")

    assert context.lookup_item_id(12, "movie") == "movie1"
    assert context.lookup_item_id(34, "episode") == "episode1"


def test_lookup_item_id_unknown_row_is_empty():
    seed_reference("movie1", 12, "movie", "Movie")
    assert context.lookup_item_id(99, "movie") == ""
    assert context.lookup_item_id(12, "episode") == ""
    assert context.lookup_item_id(0, "movie") == ""
    assert context.lookup_item_id(-1, "movie") == ""
    assert context.lookup_item_id(12, "") == ""


class FakeTag:
    def __init__(self, dbid, media_type):
        self._dbid = dbid
        self._media = media_type

    def getDbId(self):
        return self._dbid

    def getMediaType(self):
        return self._media


class FakeListItem:
    def __init__(self, prop="", dbid=-1, media_type=""):
        self._prop = prop
        self._tag = FakeTag(dbid, media_type)

    def getProperty(self, key):
        return self._prop if key == "kofin.id" else ""

    def getVideoInfoTag(self):
        return self._tag


def test_focused_item_prefers_kofin_property(monkeypatch):
    monkeypatch.setattr(sys, "listitem", FakeListItem(prop="direct1"), raising=False)
    assert context._focused_item_id() == "direct1"


def test_focused_item_falls_back_to_dbid(monkeypatch):
    seed_reference("movie1", 12, "movie", "Movie")
    monkeypatch.setattr(
        sys, "listitem", FakeListItem(dbid=12, media_type="movie"), raising=False
    )
    assert context._focused_item_id() == "movie1"


def test_focused_item_foreign_library_row(monkeypatch):
    monkeypatch.setattr(
        sys, "listitem", FakeListItem(dbid=77, media_type="movie"), raising=False
    )
    assert context._focused_item_id() == ""
