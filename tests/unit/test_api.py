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
                "kwargs": kwargs,
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


def test_probe_info_is_one_attempt_on_the_probe_budget(api):
    """The service's connect probe: the backoff loop calling it is the retry
    policy, so the transport contributes no ladder of its own. The default
    budget held the service loop ~29 s per offline probe — long enough to
    blow Kodi's five-second stop grace on a profile switch (2026-08-08)."""
    from kofin.core.api import PROBE_TIMEOUT

    client, transport = api
    client.probe_info()

    call = transport.calls[0]
    assert call["url"] == "http://s:8096/System/Info/Public"
    assert call["kwargs"]["retries"] == 0
    assert call["kwargs"]["timeout"] == PROBE_TIMEOUT


def test_played_and_favorite_verbs(api):
    client, transport = api
    client.mark_played("i1")
    client.mark_unplayed("i1")
    client.set_favorite("i1", True)
    client.set_favorite("i1", False)
    assert [(c["method"], c["url"], c["params"]) for c in transport.calls] == [
        ("POST", "http://s:8096/UserPlayedItems/i1", {"userId": "uid"}),
        ("DELETE", "http://s:8096/UserPlayedItems/i1", {"userId": "uid"}),
        ("POST", "http://s:8096/UserFavoriteItems/i1", {"userId": "uid"}),
        ("DELETE", "http://s:8096/UserFavoriteItems/i1", {"userId": "uid"}),
    ]


def test_user_scoped_calls_use_the_documented_routes(api):
    """Jellyfin 10.9 moved user-scoped routes off ``/Users/{userId}/…`` onto a
    top-level path with a ``userId`` query parameter, and 10.11 dropped the old
    shape from its OpenAPI spec while still serving it — the state a route is
    usually in right before it stops answering. Every call below was verified
    body-identical against the old one on a live 10.11.11 server.

    A ``/Users/`` path reappearing here is the regression: it works today,
    which is exactly why nothing else would catch it.
    """
    client, transport = api
    client.views()
    client.item("i1")
    client.items({"IncludeItemTypes": "Movie"})
    client.resume()
    client.special_features("i1")
    client.chapters("i1")

    for call in transport.calls:
        assert "/Users/" not in call["url"], call["url"]
        assert call["params"].get("userId") == "uid", call["url"]

    assert [c["url"].replace("http://s:8096", "") for c in transport.calls] == [
        "/UserViews",
        "/Items/i1",
        "/Items",
        "/UserItems/Resume",
        "/Items/i1/SpecialFeatures",
        "/Items/i1",
    ]


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
    assert transport.calls[0]["url"] == "http://s:8096/Items"
    assert transport.calls[0]["params"]["userId"] == "uid"
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
    """Continue watching comes off /UserItems/Resume rather than an
    IsResumable filter, so what counts as in progress -- and in what order --
    stays the server's judgement."""
    client, transport = api
    client.resume("Overview", limit=10)
    call = transport.calls[0]
    assert call["url"] == "http://s:8096/UserItems/Resume"
    assert call["params"] == {
        "userId": "uid",
        "Limit": 10,
        "MediaTypes": "Video",
        "Recursive": True,
        "EnableTotalRecordCount": False,
        "Fields": "Overview",
    }


# --- branding / splashscreen -------------------------------------------------


class ImageHttp(Http):
    """Answers with raw bytes, the way the splashscreen endpoint does."""

    def __init__(self, payload=b"\x89PNG-bytes"):
        super().__init__()
        self.payload = payload
        self.calls = []

    def request(self, method, url, headers=None, params=None, json_body=None, **kwargs):
        self.calls.append({"url": url, "headers": headers, "params": params, **kwargs})
        payload = self.payload

        class Response:
            content = payload

            def json(self):
                raise AssertionError("an image is not json")

        return Response()


def image_api(transport):
    return Api(transport, "http://s:8096", "Kodi", "dev1", "0.1.0", token="tok")


def test_splashscreen_asks_for_the_servers_own_encoding():
    """No transcode params: the endpoint ignores `quality` outright and the
    image is already 1920x1080, so asking for Jpg would only add a lossy
    generation ahead of the one Kodi's texture cache applies."""
    transport = ImageHttp()
    assert image_api(transport).splashscreen() == b"\x89PNG-bytes"

    call = transport.calls[0]
    assert call["url"] == "http://s:8096/Branding/Splashscreen"
    assert call["params"] is None
    assert call["headers"]["Accept"] == "image/*"
    # A ~2.3MB image the server may render on demand needs more than the
    # transport default read budget.
    assert call["timeout"] == (6.0, 60.0)


def test_splashscreen_returns_empty_bytes_rather_than_none():
    assert image_api(ImageHttp(payload=b"")).splashscreen() == b""


def test_branding_configuration_is_a_plain_get(api):
    client, transport = api
    client.branding_configuration()
    assert transport.calls[0]["url"] == "http://s:8096/Branding/Configuration"
    assert transport.calls[0]["method"] == "GET"


# --- interactive fail-fast profile (perf plan W1.3) --------------------------


def interactive_api():
    transport = RecordingHttp()
    client = Api(
        transport,
        "http://s:8096",
        "Kodi",
        "dev1",
        "0.1.0",
        token="tok",
        user_id="uid",
        interactive=True,
    )
    return client, transport


def test_interactive_gets_carry_one_retry_and_a_short_connect_budget():
    """A person is watching the spinner: the transport's 3x6s ladder read as
    a hang (~54s to render the root listing offline). Interactive GETs get one
    retry and a 3.05s connect budget; the read timeout stays the default."""
    client, transport = interactive_api()
    client.views()
    kwargs = transport.calls[0]["kwargs"]
    assert kwargs["retries"] == 1
    assert kwargs["timeout"] == (3.05, 30.0)


def test_interactive_list_gets_carry_the_same_budget():
    """The list-returning GETs bypass get(); the profile must reach them too
    or the who's-watching flows keep the 27s hang."""
    client, transport = interactive_api()
    client.device_sessions("dev1")
    client.users()
    client.cultures()
    for call in transport.calls:
        assert call["kwargs"]["retries"] == 1
        assert call["kwargs"]["timeout"] == (3.05, 30.0)


def test_interactive_posts_shorten_connect_but_never_gain_retries():
    """POST replay double-applies (per-method transport default); interactive
    only tightens the connect budget."""
    client, transport = interactive_api()
    client.session_playing({"ItemId": "x"})
    kwargs = transport.calls[0]["kwargs"]
    assert kwargs["timeout"] == (3.05, 30.0)
    assert "retries" not in kwargs


def test_service_profile_leaves_the_transport_defaults_alone():
    """Non-interactive callers (service, sync) prefer persistence over
    promptness: no retry or timeout override reaches the transport."""
    transport = RecordingHttp()
    client = Api(transport, "http://s:8096", "Kodi", "dev1", "0.1.0")
    client.views()
    kwargs = transport.calls[0]["kwargs"]
    assert kwargs["retries"] is None
    assert kwargs["timeout"] is None


def test_user_configuration_uses_the_top_level_routes(api):
    """``/Users/Me`` and ``/Users/Configuration`` are the exception to the
    rule the test above enforces.

    Both are top-level routes in 10.11's OpenAPI spec, not the
    ``/Users/{userId}/…`` sub-route family that was moved in 10.9 and is only
    still answered out of politeness. ``/Users/Me`` takes no user at all (the
    token names them); the write takes ``userId`` as a query parameter like
    every other user-scoped call. Verified against a live 10.11.11 server.
    """
    client, transport = api
    client.me()
    client.update_user_configuration({"SubtitleMode": "Smart", "OrderedViews": ["v1"]})

    assert [
        (c["method"], c["url"], c["params"], c["json"]) for c in transport.calls
    ] == [
        ("GET", "http://s:8096/Users/Me", None, None),
        (
            "POST",
            "http://s:8096/Users/Configuration",
            {"userId": "uid"},
            {"SubtitleMode": "Smart", "OrderedViews": ["v1"]},
        ),
    ]


def test_cultures_reads_the_localization_list(api):
    client, transport = api
    client.cultures()

    call = transport.calls[0]
    assert (call["method"], call["url"]) == (
        "GET",
        "http://s:8096/Localization/Cultures",
    )
