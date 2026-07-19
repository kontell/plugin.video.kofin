"""L1 units for the artwork-only application path (phase 5, plan §2):
image-only updates touch the art tables and the reference checksum through
the exact seam the writers use — never the full cascade — and anything
unexpected reports unhandled so the caller can fall back."""

import pytest

from kofin.sync import fields
from kofin.sync.obj import Objects
from tests.unit.fakes import FakeAddon, FakeWindow


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    Objects().mapping()


class ArtworkRecorder:
    def __init__(self):
        self.calls = []

    def add(self, artwork, kodi_id, media):
        self.calls.append((artwork, kodi_id, media))


class ReferenceRecorder:
    def __init__(self):
        self.updates = []

    def update_reference(self, *args):
        self.updates.append(args)


class FakeServer:
    server = "http://server:8096"


class FakeWriter:
    direct_path = False

    def __init__(self):
        self.server = FakeServer()
        self.objects = Objects()
        self.artwork = ArtworkRecorder()
        self.jellyfin_db = ReferenceRecorder()


def movie_item():
    return {
        "Id": "m1",
        "Type": "Movie",
        "Name": "M",
        "Etag": "E9",
        "ImageTags": {"Primary": "tag-p"},
        "BackdropImageTags": ["tag-b"],
    }


def e_item(media="movie", kodi_id=5):
    # get_item row shape: kodi_id, kodi_fileid, kodi_pathid, parent_id,
    # media_type, jellyfin_type, media_folder, jellyfin_parent_id, checksum
    return (kodi_id, 6, 7, None, media, "Movie", "lib1", None, "old|plugin")


def test_artwork_only_writes_art_and_checksum():
    writer = FakeWriter()

    assert fields.artwork_only(writer, movie_item(), e_item()) is True

    assert len(writer.artwork.calls) == 1
    artwork, kodi_id, media = writer.artwork.calls[0]
    assert kodi_id == 5 and media == "movie"
    assert "m1" in artwork["Primary"]  # server image URL for the item
    assert artwork["Backdrop"]  # backdrop list populated

    assert writer.jellyfin_db.updates == [("E9|plugin", "m1")]


def test_artwork_only_refuses_unknown_reference():
    writer = FakeWriter()
    assert fields.artwork_only(writer, movie_item(), None) is False
    assert writer.artwork.calls == []
    assert writer.jellyfin_db.updates == []


def test_artwork_only_refuses_non_video_media():
    writer = FakeWriter()
    assert fields.artwork_only(writer, movie_item(), e_item(media="album")) is False
    assert writer.artwork.calls == []


def test_artwork_only_reports_unhandled_on_error():
    writer = FakeWriter()

    def boom(*a, **kw):
        raise RuntimeError("nope")

    writer.artwork.add = boom
    assert fields.artwork_only(writer, movie_item(), e_item()) is False
    # No checksum stamp on failure: the full path must re-run cleanly.
    assert writer.jellyfin_db.updates == []


def test_artwork_only_without_etag_still_updates_art():
    writer = FakeWriter()
    item = movie_item()
    del item["Etag"]

    assert fields.artwork_only(writer, item, e_item()) is True
    assert len(writer.artwork.calls) == 1
    # No Etag -> nothing to stamp; the stale checksum stays (safe: a later
    # metadata pass sees a mismatch and re-writes).
    assert writer.jellyfin_db.updates == []
