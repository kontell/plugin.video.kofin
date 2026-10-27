"""The Downloaded folder's listing (plugin/downloaded.py): the finished song
downloads by artist, album and song, from the two local stores alone."""

from urllib.parse import parse_qs, urlsplit

import pytest
import xbmcgui
import xbmcplugin

from kofin.downloads import store as downloads
from kofin.plugin import browse, downloaded, listitems
from kofin.plugin.router import Request
from kofin.sync.backends.api import identity
from tests.unit.apifixtures import (  # noqa: F401
    ALBUM,
    ARTIST,
    album_bundle,
    song,
    store,
)
from tests.unit.fakes import FakeAddon, FakeApi

MUSIC = "music-library"
OTHER_ALBUM = "d4" * 16
OTHER_ARTIST = "f6" * 16


class RecordingListItem(xbmcgui.ListItem):
    """Keeps what the listing sets, which the stubs drop."""

    class Tag:
        def __init__(self):
            self.dbid = (0, "")
            self.values = {}

        def setDbId(self, dbid, media):
            self.dbid = (dbid, media)

        def __getattr__(self, name):
            if name.startswith("set"):
                return lambda *a, **k: self.values.__setitem__(name, a)
            raise AttributeError(name)

    def __init__(self, label="", *args, **kwargs):
        super().__init__(label, *args, **kwargs)
        self._label = label
        self._props = {}
        self._art = {}
        self._tag = self.Tag()

    def getLabel(self):
        return self._label

    def setProperty(self, key, value):
        self._props[key] = value

    def getProperty(self, key):
        return self._props.get(key, "")

    def setArt(self, dictionary):
        self._art.update(dictionary)

    def getArt(self, key):
        return self._art.get(key, "")

    def getMusicInfoTag(self):
        return self._tag


@pytest.fixture
def listing(store, monkeypatch):
    """The route against the catalogue and the download store, with what it
    hands Kodi captured. The api is a FakeApi with no methods configured:
    a server call would raise."""
    FakeAddon.store = {"maxArtResolution": "0", "compressArt": "false"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr(xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(listitems.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(identity, "current_store", lambda: store)
    api = FakeApi()
    monkeypatch.setattr(browse, "_api", lambda: api)
    captured = {"entries": [], "content": None, "ended": None, "sorts": []}
    monkeypatch.setattr(
        xbmcplugin,
        "addDirectoryItems",
        lambda handle, entries, count: captured["entries"].extend(entries) or True,
    )
    monkeypatch.setattr(
        xbmcplugin, "setContent", lambda h, c: captured.__setitem__("content", c)
    )
    monkeypatch.setattr(
        xbmcplugin,
        "endOfDirectory",
        lambda h, succeeded=True, **kw: captured.__setitem__("ended", succeeded),
    )
    monkeypatch.setattr(
        xbmcplugin, "addSortMethod", lambda h, m: captured["sorts"].append(m)
    )

    def run(**params):
        captured["entries"].clear()
        captured["sorts"].clear()
        params.setdefault("mode", "downloadedmusic")
        downloaded.music(Request("plugin://x", 1, params))
        return captured

    run.api = api  # type: ignore[attr-defined]
    return run


def _finished(item_id, album_id=ALBUM):
    downloads.queue(
        downloads.Download(jellyfin_id=item_id, media_type="song", series_id=album_id)
    )
    downloads.finish(item_id, "Music/%s/%s.flac" % (album_id, item_id), "flac", 1000)


def _filed(store, item_id, kodi_id):
    record = store.records(pinned=False, item_ids=[item_id])[item_id]
    assert store.remember(item_id, record.generation, kodi_id, {}, record.kind)


def _query(path):
    return {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}


def test_the_songs_level_lists_the_finished_downloads_the_catalogue_places(
    store, listing
):
    """Two of three songs are downloaded, and a fourth finished download the
    catalogue never placed is not listed: there is no payload to build it
    from. Rows are built from the payload with the Kodi id the row was
    acknowledged against, and play through the plugin URL, which the
    resolver answers from disk."""
    items = album_bundle()
    store.publish(items, library=MUSIC)
    songs = [i for i in items if i["Type"] == "Audio"]
    _finished(songs[2]["Id"])
    _finished(songs[0]["Id"])
    _finished("ghost")
    _filed(store, songs[0]["Id"], 41)

    captured = listing(content="songs")

    assert captured["ended"] is True and captured["content"] == "songs"
    rows = captured["entries"]
    assert [li.getLabel() for _p, li, _f in rows] == ["Track 1", "Track 3"]
    assert [folder for _p, _li, folder in rows] == [False, False]
    assert [_query(path)["id"] for path, _li, _f in rows] == [
        songs[0]["Id"],
        songs[2]["Id"],
    ]
    assert all(_query(path)["mode"] == "play" for path, _li, _f in rows)
    assert [li.getProperty("kofin.id") for _p, li, _f in rows] == [
        songs[0]["Id"],
        songs[2]["Id"],
    ]
    # The filed song reads as the library's row; the other is not yet.
    assert rows[0][1].getMusicInfoTag().dbid == (41, "song")
    assert rows[1][1].getMusicInfoTag().dbid == (0, "")
    assert xbmcplugin.SORT_METHOD_TRACKNUM in captured["sorts"]
    # Nothing was asked of the server.
    assert listing.api.calls == []


def test_albums_and_artists_group_the_songs_and_narrow_the_next_level(store, listing):
    """An album is listed once for its downloaded songs, under its catalogue
    record (art, year, Kodi id) when it has one; an artist is the album
    artist of the songs; each row opens the next level narrowed to it."""
    first = album_bundle()
    second = album_bundle(album_id=OTHER_ALBUM, artist_id=OTHER_ARTIST, prefix="u")
    store.publish(first + second, library=MUSIC)
    for item in first + second:
        if item["Type"] == "Audio" and item["IndexNumber"] < 3:
            _finished(item["Id"], item["AlbumId"])
    _filed(store, ALBUM, 7)
    _filed(store, ARTIST, 3)

    albums = listing(content="albums")
    assert albums["content"] == "albums"
    assert [li.getLabel() for _p, li, _f in albums["entries"]] == [
        "Record",  # Act d4 sorts after Band
        "Record",
    ]
    by_album = {_query(p)["album"]: (p, li, f) for p, li, f in albums["entries"]}
    assert set(by_album) == {ALBUM, OTHER_ALBUM}
    path, li, folder = by_album[ALBUM]
    assert folder is True
    assert _query(path) == {
        "mode": "downloadedmusic",
        "content": "songs",
        "album": ALBUM,
    }
    assert li.getMusicInfoTag().dbid == (7, "album")
    assert li.getArt("thumb").endswith("/Items/%s/Images/Primary?tag=cover" % ALBUM)

    artists = listing(content="artists")
    assert artists["content"] == "artists"
    assert [li.getLabel() for _p, li, _f in artists["entries"]] == ["Act f6", "Band"]
    band = [row for row in artists["entries"] if row[1].getLabel() == "Band"][0]
    assert _query(band[0]) == {
        "mode": "downloadedmusic",
        "content": "albums",
        "artist": ARTIST,
    }
    assert band[1].getMusicInfoTag().dbid == (3, "artist")

    narrowed = listing(content="albums", artist=OTHER_ARTIST)
    assert [_query(p)["album"] for p, _li, _f in narrowed["entries"]] == [OTHER_ALBUM]
    assert _query(narrowed["entries"][0][0])["artist"] == OTHER_ARTIST

    songs = listing(content="songs", album=OTHER_ALBUM)
    assert [li.getLabel() for _p, li, _f in songs["entries"]] == ["Track 1", "Track 2"]
    assert listing.api.calls == []


def test_an_album_the_catalogue_does_not_record_is_built_from_its_song(store, listing):
    """A song whose album has no catalogue record (the listing left it out)
    still stands under an album row, made from the song's own fields; a
    song with no album at all is listed among the songs and under its
    artist, but has no album row to stand under."""
    loose = song("solo", album_id=None, number=1)
    orphan = song(
        "orph", album_id="9" * 32, number=2, Album="Lost", AlbumPrimaryImageTag="lp"
    )
    store.publish([loose, orphan], library=MUSIC)
    _finished("solo", "")
    _finished("orph", "9" * 32)

    albums = listing(content="albums")
    assert [li.getLabel() for _p, li, _f in albums["entries"]] == ["Lost"]
    assert (
        albums["entries"][0][1]
        .getArt("thumb")
        .endswith("/Items/%s/Images/Primary?tag=lp" % ("9" * 32))
    )
    songs = listing(content="songs")
    assert [li.getLabel() for _p, li, _f in songs["entries"]] == ["Track 1", "Track 2"]
    artists = listing(content="artists")
    assert [li.getLabel() for _p, li, _f in artists["entries"]] == ["Band"]


def test_nothing_downloaded_is_an_empty_listing_and_logged_out_a_failed_one(
    store, listing, monkeypatch
):
    captured = listing(content="artists")
    assert captured["entries"] == [] and captured["ended"] is True
    monkeypatch.setattr(browse, "_api", lambda: None)
    captured = listing(content="songs")
    assert captured["entries"] == [] and captured["ended"] is False
