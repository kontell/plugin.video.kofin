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
