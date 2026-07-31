import pytest

from kofin.core.api import Api
from kofin.core.http import Http


class RecordingHttp(Http):
    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, headers=None, params=None, json_body=None, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json_body,
            }
        )

        class Response:
            content = b""

            def json(self):
                return {}

        return Response()


@pytest.fixture
def api():
    transport = RecordingHttp()
    client = Api(
        transport,
        "http://s:8096",
        "Kodi",
        "dev1",
        "0.1.0",
        token="tok",
        user_id="uid",
    )
    return client, transport


def test_urls_and_auth_header(api):
    client, transport = api
    client.get("System/Info/Public")
    call = transport.calls[0]
    assert call["url"] == "http://s:8096/System/Info/Public"
    assert 'Token="tok"' in call["headers"]["Authorization"]


def test_played_and_favorite_verbs(api):
    client, transport = api
    client.mark_played("i1")
    client.mark_unplayed("i1")
    client.set_favorite("i1", True)
    client.set_favorite("i1", False)
    verbs = [(c["method"], c["url"].rsplit("/", 3)[-3:]) for c in transport.calls]
    assert verbs[0][0] == "POST" and "PlayedItems" in transport.calls[0]["url"]
    assert verbs[1][0] == "DELETE"
    assert verbs[2][0] == "POST" and "FavoriteItems" in transport.calls[2]["url"]
    assert verbs[3][0] == "DELETE"


def test_delete_item(api):
    client, transport = api
    client.delete_item("i1")
    delete_call = transport.calls[0]
    assert delete_call["method"] == "DELETE"
    assert delete_call["url"] == "http://s:8096/Items/i1"


def test_music_playlists_and_items(api):
    client, transport = api

    class Response:
        def __init__(self, body):
            self.content = b"{}"
            self._body = body

        def json(self):
            return self._body

    # First call: list playlists (one page, includes a video playlist to filter)
    # Second call: playlist items page
    bodies = [
        {
            "Items": [
                {"Id": "a", "Name": "Gym", "MediaType": "Audio", "Type": "Playlist"},
                {"Id": "v", "Name": "Movies", "MediaType": "Video", "Type": "Playlist"},
                {"Id": "e", "Name": "Empty", "MediaType": "", "Type": "Playlist"},
            ],
            "TotalRecordCount": 3,
        },
        {
            "Items": [{"Id": "s1", "Type": "Audio", "Name": "Song"}],
            "TotalRecordCount": 1,
        },
    ]
    idx = {"n": 0}

    def request(method, url, headers=None, params=None, json_body=None, **kwargs):
        transport.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json_body,
            }
        )
        body = bodies[idx["n"]]
        idx["n"] += 1
        return Response(body)

    transport.request = request  # type: ignore[method-assign]

    listed = client.music_playlists()
    assert [p["Id"] for p in listed] == ["a", "e"]
    assert "/Users/uid/Items" in transport.calls[0]["url"]
    assert transport.calls[0]["params"]["IncludeItemTypes"] == "Playlist"

    items = client.playlist_items("a")
    assert items["Items"][0]["Id"] == "s1"
    assert transport.calls[1]["url"].endswith("/Playlists/a/Items")
    assert transport.calls[1]["params"]["UserId"] == "uid"


def test_music_playlists_dedupes_repeated_pages(api):
    """Jellyfin can over-report TotalRecordCount and re-emit rows on page 2."""
    client, transport = api

    class Response:
        def __init__(self, body):
            self.content = b"{}"
            self._body = body

        def json(self):
            return self._body

    page1 = {
        "Items": [
            {"Id": "a", "Name": "Shuffle 02", "MediaType": "Audio"},
            {"Id": "b", "Name": "Leo", "MediaType": "Audio"},
        ],
        "TotalRecordCount": 5,
    }
    page2 = {
        "Items": [
            {"Id": "a", "Name": "Shuffle 02", "MediaType": "Audio"},
            {"Id": "b", "Name": "Leo", "MediaType": "Audio"},
        ],
        "TotalRecordCount": 5,
    }
    pages = [page1, page2]
    idx = {"n": 0}

    def request(method, url, headers=None, params=None, json_body=None, **kwargs):
        transport.calls.append({"params": params})
        body = pages[min(idx["n"], len(pages) - 1)]
        idx["n"] += 1
        return Response(body)

    transport.request = request  # type: ignore[method-assign]
    listed = client.music_playlists()
    assert [p["Id"] for p in listed] == ["a", "b"]
    # Stopped after the repeated page (no third request chasing the fake total).
    assert idx["n"] == 2


def test_playback_info_optional_params(api):
    client, transport = api
    client.playback_info("item1", {"Name": "Kodi"})
    first = transport.calls[0]
    assert "MaxStreamingBitrate" not in first["params"]
    assert first["json"]["DeviceProfile"] == {"Name": "Kodi"}

    client.playback_info("item1", {}, max_bitrate=8_000_000, audio_index=2)
    second = transport.calls[1]
    assert second["params"]["MaxStreamingBitrate"] == 8_000_000
    assert second["params"]["AudioStreamIndex"] == 2


def test_image_url(api):
    client, _ = api
    assert (
        client.image_url("i1", "Backdrop", "t9")
        == "http://s:8096/Items/i1/Images/Backdrop?tag=t9"
    )


def test_syncplay_endpoints(api):
    """The 17 /SyncPlay/* calls + /GetUtcTime (phase 4) hit the documented
    routes with the documented body shapes."""
    client, transport = api
    client.get_utc_time()
    client.syncplay_list()
    client.syncplay_new("movie night")
    client.syncplay_join("g1")
    client.syncplay_leave()
    client.syncplay_ready("2026-07-19T00:00:00.000Z", 150000000, False, "pl-1")
    client.syncplay_buffering("2026-07-19T00:00:00.000Z", 0, False, "pl-1")
    client.syncplay_ping(23)
    client.syncplay_unpause()
    client.syncplay_pause()
    client.syncplay_stop()
    client.syncplay_seek(420000000)
    client.syncplay_set_new_queue(["i1"], 0, 900000000)
    client.syncplay_set_playlist_item("pl-2")
    client.syncplay_queue(["i2"], "QueueNext")
    client.syncplay_next_item("pl-1")
    client.syncplay_previous_item("pl-1")
    client.syncplay_set_ignore_wait(True)

    calls = {call["url"].replace("http://s:8096", ""): call for call in transport.calls}
    assert calls["/GetUtcTime"]["method"] == "GET"
    assert calls["/SyncPlay/List"]["method"] == "GET"
    assert calls["/SyncPlay/New"]["json"] == {"GroupName": "movie night"}
    assert calls["/SyncPlay/Join"]["json"] == {"GroupId": "g1"}
    assert calls["/SyncPlay/Leave"]["method"] == "POST"
    ready = calls["/SyncPlay/Ready"]["json"]
    assert ready == {
        "When": "2026-07-19T00:00:00.000Z",
        "PositionTicks": 150000000,
        "IsPlaying": False,
        "PlaylistItemId": "pl-1",
    }
    assert set(calls["/SyncPlay/Buffering"]["json"]) == set(ready)
    assert calls["/SyncPlay/Ping"]["json"] == {"Ping": 23}
    assert calls["/SyncPlay/Unpause"]["method"] == "POST"
    assert calls["/SyncPlay/Pause"]["method"] == "POST"
    assert calls["/SyncPlay/Stop"]["method"] == "POST"
    assert calls["/SyncPlay/Seek"]["json"] == {"PositionTicks": 420000000}
    assert calls["/SyncPlay/SetNewQueue"]["json"] == {
        "PlayingQueue": ["i1"],
        "PlayingItemPosition": 0,
        "StartPositionTicks": 900000000,
    }
    assert calls["/SyncPlay/SetPlaylistItem"]["json"] == {"PlaylistItemId": "pl-2"}
    assert calls["/SyncPlay/Queue"]["json"] == {"ItemIds": ["i2"], "Mode": "QueueNext"}
    assert calls["/SyncPlay/NextItem"]["json"] == {"PlaylistItemId": "pl-1"}
    assert calls["/SyncPlay/PreviousItem"]["json"] == {"PlaylistItemId": "pl-1"}
    assert calls["/SyncPlay/SetIgnoreWait"]["json"] == {"IgnoreWait": True}


def test_resume_asks_the_server_for_its_own_list(api):
    """Continue watching comes off /Items/Resume rather than an IsResumable
    filter, so what counts as in progress -- and in what order -- stays the
    server's judgement."""
    client, transport = api
    client.resume("Overview", limit=10)
    call = transport.calls[0]
    assert call["url"] == "http://s:8096/Users/uid/Items/Resume"
    assert call["params"] == {
        "Limit": 10,
        "MediaTypes": "Video",
        "Recursive": True,
        "EnableTotalRecordCount": False,
        "Fields": "Overview",
    }
