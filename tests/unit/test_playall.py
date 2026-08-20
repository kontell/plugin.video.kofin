"""L1: Play all / Shuffle over a music container (plugin/playall.py).

Kodi offers no Play on a plugin folder, so the route is the only way an
album, artist, genre or playlist row plays as a whole. What is under test is
the expansion -- which query, which order, how much -- and the hand-over:
a *music* playlist, stopped-before-played.
"""

import pytest
import xbmc

from kofin.core.http import JellyfinError
from kofin.plugin import playall
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon


class FakePlaylist:
    instances = []

    def __init__(self, kind):
        self.kind = kind
        self.entries = []
        self.cleared = 0
        FakePlaylist.instances.append(self)

    def clear(self):
        self.cleared += 1
        self.entries = []

    def add(self, url, listitem=None, index=-1):
        self.entries.append((url, listitem))


class FakePlayer:
    played = []

    def play(self, item=None, listitem=None, windowed=False, startpos=-1):
        FakePlayer.played.append(item)


class ContainerApi:
    server = "http://server:8096"

    def __init__(self, item, tracks, total=None, fail=False):
        self._item = item
        self.tracks = tracks
        self.total = total
        self.fail = fail
        self.items_params = []
        self.playlist_calls = []

    def item(self, item_id):
        if self.fail:
            raise JellyfinError("down")
        return self._item

    def items(self, params):
        self.items_params.append(params)
        return {
            "Items": list(self.tracks),
            "TotalRecordCount": (
                self.total if self.total is not None else len(self.tracks)
            ),
        }

    def playlist_items(self, playlist_id, start_index=0, limit=100, fields=""):
        self.playlist_calls.append((playlist_id, start_index, limit, fields))
        return {
            "Items": self.tracks[start_index : start_index + limit],
            "TotalRecordCount": len(self.tracks),
        }


def track(index, item_type="Audio"):
    return {
        "Id": "t%d" % index,
        "Name": "Track %d" % index,
        "Type": item_type,
        "IndexNumber": index,
        "ImageTags": {},
    }


TRACKS = [track(1), track(2), track(3)]


@pytest.fixture(autouse=True)
def kodi(monkeypatch):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    FakePlaylist.instances = []
    FakePlayer.played = []
    monkeypatch.setattr(playall.xbmc, "PlayList", FakePlaylist)
    monkeypatch.setattr(playall.xbmc, "Player", FakePlayer)
    events = []
    monkeypatch.setattr(playall, "stop_current_playback", lambda: events.append("stop"))
    monkeypatch.setattr(playall.toast, "show", lambda *a, **k: events.append("toast"))
    return events


def _play(monkeypatch, api, shuffle=False):
    monkeypatch.setattr(playall, "_api", lambda: api)
    params = {"mode": "playall", "id": api._item["Id"]}
    if shuffle:
        params["shuffle"] = "1"
    playall.play_all(Request("plugin://x", -1, params))


def test_an_album_plays_in_disc_and_track_order(monkeypatch, kodi):
    api = ContainerApi({"Id": "al1", "Type": "MusicAlbum"}, TRACKS)

    _play(monkeypatch, api)

    query = api.items_params[0]
    assert query["ParentId"] == "al1"
    assert query["IncludeItemTypes"] == "Audio"
    assert query["SortBy"] == "ParentIndexNumber,IndexNumber,SortName"
    assert query["Limit"] == playall.QUEUE_LIMIT
    assert query["EnableTotalRecordCount"] is True

    playlist = FakePlaylist.instances[-1]
    assert playlist.kind == xbmc.PLAYLIST_MUSIC  # never the video playlist
    assert playlist.cleared == 1
    assert [url for url, _li in playlist.entries] == [
        "plugin://plugin.video.kofin/?mode=play&id=t1",
        "plugin://plugin.video.kofin/?mode=play&id=t2",
        "plugin://plugin.video.kofin/?mode=play&id=t3",
    ]
    assert all(li is not None for _url, li in playlist.entries)
    assert kodi == ["stop"]  # stopped before the hand-over, nothing toasted
    assert FakePlayer.played == [playlist]


def test_shuffle_asks_the_server_for_a_random_order(monkeypatch):
    api = ContainerApi({"Id": "al1", "Type": "MusicAlbum"}, TRACKS)
    _play(monkeypatch, api, shuffle=True)
    assert api.items_params[0]["SortBy"] == "Random"


def test_an_artist_is_a_link_target_and_a_genre_a_filter(monkeypatch):
    artist = ContainerApi({"Id": "ar1", "Type": "MusicArtist"}, TRACKS)
    _play(monkeypatch, artist)
    assert artist.items_params[0]["ArtistIds"] == "ar1"
    assert "ParentId" not in artist.items_params[0]
    assert artist.items_params[0]["SortBy"].startswith("ProductionYear,Album,")

    genre = ContainerApi({"Id": "g1", "Type": "MusicGenre"}, TRACKS)
    _play(monkeypatch, genre)
    assert genre.items_params[0]["GenreIds"] == "g1"
    assert genre.items_params[0]["SortBy"].startswith("AlbumArtist,Album,")


def test_a_playlist_keeps_the_servers_order_and_its_audio_only(monkeypatch):
    """The playlist route has no SortBy -- the order *is* the playlist -- and a
    mixed playlist contributes only its tracks."""
    rows = [track(1), track(2, "Movie"), track(3)]
    api = ContainerApi({"Id": "p1", "Type": "Playlist", "MediaType": "Audio"}, rows)

    _play(monkeypatch, api)

    assert api.items_params == []
    assert api.playlist_calls == [("p1", 0, playall.PAGE_SIZE, playall.TRACK_FIELDS)]
    urls = [url for url, _li in FakePlaylist.instances[-1].entries]
    assert urls == [
        "plugin://plugin.video.kofin/?mode=play&id=t1",
        "plugin://plugin.video.kofin/?mode=play&id=t3",
    ]


def test_a_playlist_shuffles_here_because_the_server_cannot(monkeypatch):
    api = ContainerApi({"Id": "p1", "Type": "Playlist", "MediaType": "Audio"}, TRACKS)
    monkeypatch.setattr(playall.random, "shuffle", lambda rows: rows.reverse())

    _play(monkeypatch, api, shuffle=True)

    urls = [url for url, _li in FakePlaylist.instances[-1].entries]
    assert urls == [
        "plugin://plugin.video.kofin/?mode=play&id=t3",
        "plugin://plugin.video.kofin/?mode=play&id=t2",
        "plugin://plugin.video.kofin/?mode=play&id=t1",
    ]


def test_a_long_playlist_is_paged_up_to_the_cap(monkeypatch):
    rows = [track(index) for index in range(1, 251)]
    api = ContainerApi({"Id": "p1", "Type": "Playlist", "MediaType": "Audio"}, rows)

    _play(monkeypatch, api)

    assert [start for _id, start, _limit, _fields in api.playlist_calls] == [
        0,
        100,
        200,
    ]
    assert len(FakePlaylist.instances[-1].entries) == 250


def test_nothing_playable_queues_nothing(monkeypatch, kodi):
    api = ContainerApi({"Id": "al1", "Type": "MusicAlbum"}, [])
    _play(monkeypatch, api)
    assert FakePlaylist.instances == []
    assert FakePlayer.played == []
    assert kodi == []


def test_a_video_container_is_refused(monkeypatch, kodi):
    """Music only, by decision: a season reaching the route by hand gets no
    queue, not an episode queue."""
    api = ContainerApi({"Id": "s1", "Type": "Season"}, TRACKS)
    _play(monkeypatch, api)
    assert api.items_params == []
    assert FakePlaylist.instances == []
    assert kodi == []


def test_a_server_failure_toasts_and_plays_nothing(monkeypatch, kodi):
    api = ContainerApi({"Id": "al1", "Type": "MusicAlbum"}, TRACKS, fail=True)
    _play(monkeypatch, api)
    assert FakePlaylist.instances == []
    assert kodi == ["toast"]


def test_logged_out_does_nothing(monkeypatch):
    monkeypatch.setattr(playall, "_api", lambda: None)
    playall.play_all(Request("plugin://x", -1, {"mode": "playall", "id": "al1"}))
    assert FakePlaylist.instances == []
