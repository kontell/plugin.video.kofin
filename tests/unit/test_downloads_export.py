"""L1 units for the NFO/artwork export (plan W4.3)."""

from kofin.downloads import export


class FakeExportApi:
    def __init__(self, series=None, items=None):
        self._series = series or {}
        # Per-id answers, for the music legs: one export can look up an
        # album and an artist, and they are not the same document.
        self._items = items or {}
        self.item_calls = []
        self.downloads = []

    def item(self, item_id):
        self.item_calls.append(item_id)
        if self._items:
            return self._items.get(item_id, {})
        return self._series

    def image_url(self, item_id, image_type="Primary", tag=""):
        return "img://%s/%s/%s" % (item_id, image_type, tag)

    def download(self, url):
        self.downloads.append(url)
        return b"IMAGEBYTES"


MOVIE = {
    "Id": "m1",
    "Type": "Movie",
    "Name": "Heat & Dust",  # the ampersand must escape
    "ProductionYear": 1995,
    "PremiereDate": "1995-12-15T00:00:00.0000000Z",
    "Overview": "A film.",
    "ProviderIds": {"Tmdb": "949", "Imdb": "tt0113277"},
    "ImageTags": {"Primary": "ptag"},
    "BackdropImageTags": ["btag"],
}


def test_movie_export_writes_nfo_and_art(tmp_path):
    directory = tmp_path / "Movies" / "Heat (1995)"
    directory.mkdir(parents=True)
    media = directory / "Heat (1995).mkv"
    media.write_bytes(b"x")
    api = FakeExportApi()

    export.export_item(api, MOVIE, str(media))

    nfo = (directory / "Heat (1995).nfo").read_text(encoding="utf-8")
    assert "<movie>" in nfo and "<title>Heat &amp; Dust</title>" in nfo
    assert "<year>1995</year>" in nfo and "<premiered>1995-12-15</premiered>" in nfo
    # uniqueids present, imdb defaulted by priority whatever the dict order.
    assert '<uniqueid type="imdb" default="true">tt0113277</uniqueid>' in nfo
    assert '<uniqueid type="tmdb">949</uniqueid>' in nfo
    assert (directory / "poster.jpg").read_bytes() == b"IMAGEBYTES"
    assert (directory / "fanart.jpg").read_bytes() == b"IMAGEBYTES"


def test_art_is_never_refetched_over_an_existing_file(tmp_path):
    directory = tmp_path / "Movies" / "M"
    directory.mkdir(parents=True)
    (directory / "poster.jpg").write_bytes(b"theirs")
    media = directory / "M.mkv"
    media.write_bytes(b"x")
    api = FakeExportApi()

    export.export_item(api, MOVIE, str(media))

    assert (directory / "poster.jpg").read_bytes() == b"theirs"
    assert not any("Primary" in url for url in api.downloads)


EPISODE = {
    "Id": "e1",
    "Type": "Episode",
    "Name": "Pilot",
    "SeriesName": "The Show",
    "SeriesId": "s1",
    "ParentIndexNumber": 1,
    "IndexNumber": 1,
    "PremiereDate": "2011-04-17T00:00:00.0000000Z",
    "Overview": "It begins.",
    "ProviderIds": {"Tvdb": "3254641"},
    "SeriesPrimaryImageTag": "stag",
    "ParentBackdropItemId": "s1",
    "ParentBackdropImageTags": ["sbtag"],
}

SERIES = {
    "Id": "s1",
    "Name": "The Show",
    "ProductionYear": 2011,
    "Overview": "A show.",
    "ProviderIds": {"Tvdb": "121361", "Imdb": "tt0944947"},
}


def test_episode_export_writes_show_level_files_once(tmp_path):
    season = tmp_path / "TV" / "The Show" / "Season 01"
    season.mkdir(parents=True)
    first = season / "S01E01 Pilot.mkv"
    first.write_bytes(b"x")
    api = FakeExportApi(series=SERIES)

    export.export_item(api, EPISODE, str(first))

    nfo = (season / "S01E01 Pilot.nfo").read_text(encoding="utf-8")
    assert "<episodedetails>" in nfo
    assert "<season>1</season>" in nfo and "<episode>1</episode>" in nfo
    assert "<aired>2011-04-17</aired>" in nfo
    show_dir = tmp_path / "TV" / "The Show"
    show_nfo = (show_dir / "tvshow.nfo").read_text(encoding="utf-8")
    assert "<tvshow>" in show_nfo
    assert '<uniqueid type="imdb" default="true">tt0944947</uniqueid>' in show_nfo
    assert (show_dir / "poster.jpg").exists() and (show_dir / "fanart.jpg").exists()
    assert api.item_calls == ["s1"]

    second = season / "S01E02 Next.mkv"
    second.write_bytes(b"x")
    export.export_item(api, dict(EPISODE, Id="e2", IndexNumber=2), str(second))
    assert api.item_calls == ["s1"]  # tvshow.nfo present: no second fetch


def test_a_seasonless_episode_treats_its_directory_as_the_show(tmp_path):
    show_dir = tmp_path / "TV" / "The Show"
    show_dir.mkdir(parents=True)
    media = show_dir / "Special.mkv"
    media.write_bytes(b"x")
    api = FakeExportApi(series=SERIES)

    export.export_item(
        api, dict(EPISODE, ParentIndexNumber=None, IndexNumber=None), str(media)
    )

    assert (show_dir / "tvshow.nfo").exists()
    assert not (tmp_path / "TV" / "tvshow.nfo").exists()  # never a level up


def test_song_export_writes_the_album_folder_image(tmp_path):
    album = tmp_path / "Music" / "Artist" / "Album"
    album.mkdir(parents=True)
    media = album / "01 Track.opus"
    media.write_bytes(b"x")
    api = FakeExportApi()

    song = {
        "Id": "a1",
        "Type": "Audio",
        "AlbumId": "al1",
        "AlbumPrimaryImageTag": "atag",
    }
    export.export_item(api, song, str(media))

    assert (album / "folder.jpg").read_bytes() == b"IMAGEBYTES"
    assert api.downloads == ["img://al1/Primary/atag"]


SONG = {
    "Id": "a1",
    "Type": "Audio",
    "AlbumId": "al1",
    "AlbumPrimaryImageTag": "atag",
    "AlbumArtist": "Ella & Louis",
    "AlbumArtists": [{"Id": "ar1", "Name": "Ella & Louis"}],
}

ALBUM = {
    "Id": "al1",
    "Type": "MusicAlbum",
    "Name": "Porgy & Bess",  # the ampersand must escape
    "AlbumArtist": "Ella & Louis",
    "AlbumArtists": [{"Id": "ar1", "Name": "Ella Fitzgerald"}],
    "ProductionYear": 1957,
    "Genres": ["Jazz", "Vocal"],
    "Overview": "A record.",
    "ProviderIds": {
        "MusicBrainzAlbum": "mb-album",
        "MusicBrainzReleaseGroup": "mb-group",
    },
}

ARTIST = {
    "Id": "ar1",
    "Type": "MusicArtist",
    "Name": "Ella Fitzgerald",
    "Genres": ["Jazz"],
    "Overview": "A singer.",
    "ProviderIds": {"MusicBrainzArtist": "mb-artist"},
    "ImageTags": {"Primary": "aptag"},
    "BackdropImageTags": ["abtag"],
}


def _music_api():
    return FakeExportApi(items={"al1": ALBUM, "ar1": ARTIST})


def test_song_export_writes_album_and_artist_metadata(tmp_path):
    """D5: an album download used to leave one file, ``folder.jpg``."""
    album_dir = tmp_path / "Music" / "Ella & Louis" / "Porgy & Bess"
    album_dir.mkdir(parents=True)
    media = album_dir / "01 Track.opus"
    media.write_bytes(b"x")
    artist_dir = album_dir.parent
    api = _music_api()

    export.export_item(api, SONG, str(media))

    assert (album_dir / "folder.jpg").read_bytes() == b"IMAGEBYTES"
    assert (artist_dir / "folder.jpg").read_bytes() == b"IMAGEBYTES"
    assert (artist_dir / "fanart.jpg").read_bytes() == b"IMAGEBYTES"

    album_text = (album_dir / "album.nfo").read_text(encoding="utf-8")
    assert "<title>Porgy &amp; Bess</title>" in album_text
    assert "<artistdesc>Ella &amp; Louis</artistdesc>" in album_text
    assert "<artist>Ella Fitzgerald</artist>" in album_text
    assert "<genre>Jazz</genre>" in album_text and "<genre>Vocal</genre>" in album_text
    assert "<year>1957</year>" in album_text
    # Lower case, because that is how CAlbum::Load spells it and XMLUtils
    # matches case-sensitively — a camel-cased tag here is silently dropped.
    assert "<musicbrainzalbumid>mb-album</musicbrainzalbumid>" in album_text
    assert "<musicbrainzreleasegroupid>mb-group</musicbrainzreleasegroupid>" in (
        album_text
    )

    artist_text = (artist_dir / "artist.nfo").read_text(encoding="utf-8")
    assert "<name>Ella Fitzgerald</name>" in artist_text
    assert "<biography>A singer.</biography>" in artist_text
    # ...and camel case here, because CArtist::Load spells this one that way.
    assert "<musicBrainzArtistID>mb-artist</musicBrainzArtistID>" in artist_text
    # Neither music document carries <uniqueid>: Kodi's music side has no
    # uniqueid table and reads only the named MusicBrainz elements.
    assert "uniqueid" not in album_text and "uniqueid" not in artist_text


def test_song_export_looks_each_level_up_once_per_album(tmp_path):
    """One album fetch and one artist fetch for a whole album, not one of
    each per track — the rule the show level already follows."""
    album_dir = tmp_path / "Music" / "Ella & Louis" / "Porgy & Bess"
    album_dir.mkdir(parents=True)
    api = _music_api()

    for index in range(1, 4):
        media = album_dir / ("%02d Track.opus" % index)
        media.write_bytes(b"x")
        export.export_item(api, SONG, str(media))

    assert api.item_calls == ["al1", "ar1"]


def test_song_export_without_an_artist_entity_writes_nothing_artist_level(tmp_path):
    """A track whose album artist is only a tag has nothing to look up, and
    must not cost a fetch or leave a half-written artist directory."""
    album_dir = tmp_path / "Music" / "Some Band" / "Some Album"
    album_dir.mkdir(parents=True)
    media = album_dir / "01 Track.opus"
    media.write_bytes(b"x")
    api = _music_api()
    song = dict(SONG)
    song.pop("AlbumArtists")

    export.export_item(api, song, str(media))

    assert api.item_calls == ["al1"]  # the album, never an artist
    assert not (album_dir.parent / "artist.nfo").exists()
    assert not (album_dir.parent / "folder.jpg").exists()
    assert (album_dir / "album.nfo").exists()


def test_song_export_keeps_the_cover_when_the_lookups_fail(tmp_path):
    """Album art needs no fetch, so a server that cannot answer the album or
    artist lookup must not cost the track the one file it could have had."""

    class HalfDeadApi(FakeExportApi):
        def item(self, item_id):
            self.item_calls.append(item_id)
            raise RuntimeError("boom")

    album_dir = tmp_path / "Music" / "Ella & Louis" / "Porgy & Bess"
    album_dir.mkdir(parents=True)
    media = album_dir / "01 Track.opus"
    media.write_bytes(b"x")
    api = HalfDeadApi()

    export.export_item(api, SONG, str(media))  # never raises

    assert (album_dir / "folder.jpg").read_bytes() == b"IMAGEBYTES"
    assert not (album_dir / "album.nfo").exists()


def test_export_never_raises(tmp_path):
    class ExplodingApi:
        def item(self, item_id):
            raise RuntimeError("boom")

        def image_url(self, *a, **k):
            raise RuntimeError("boom")

        def download(self, url):
            raise RuntimeError("boom")

    media = tmp_path / "f.mkv"
    media.write_bytes(b"x")
    export.export_item(ExplodingApi(), dict(MOVIE), str(media))  # no raise
    export.export_item(FakeExportApi(), {"Type": "Photo"}, str(media))  # no-op
