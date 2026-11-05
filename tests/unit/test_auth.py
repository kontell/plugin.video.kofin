from urllib.parse import unquote_plus

import pytest

from kofin.core import auth


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("minipie", "http://minipie:8096"),
        ("192.168.1.167", "http://192.168.1.167:8096"),
        ("minipie:9000", "http://minipie:9000"),
        ("http://minipie/", "http://minipie:8096"),
        ("https://jelly.konell.xyz", "https://jelly.konell.xyz"),
        ("https://jelly.konell.xyz/", "https://jelly.konell.xyz"),
        ("https://host/jellyfin/", "https://host/jellyfin"),
        ("  minipie  ", "http://minipie:8096"),
        ("", ""),
        # An IPv6 literal is full of colons; the default port still lands
        # after the bracket.
        ("[fd00::1]", "http://[fd00::1]:8096"),
        ("http://[fd00::1]", "http://[fd00::1]:8096"),
        ("[fd00::1]:8920", "http://[fd00::1]:8920"),
        ("https://[fd00::1]", "https://[fd00::1]"),
    ],
)
def test_normalize_address(raw, expected):
    assert auth.normalize_address(raw) == expected


def test_auth_header_without_token():
    header = auth.build_auth_header("Living Room", "dev123", "0.1.0")
    assert header == (
        'MediaBrowser Client="Kofin", Device="Living%20Room", '
        'DeviceId="dev123", Version="0.1.0"'
    )


def test_auth_header_with_token_and_quote_escape():
    header = auth.build_auth_header('The "Box", 2', "d", "1", token="tok")
    assert 'Device="The%20%22Box%22%2C%202"' in header
    assert header.endswith('Token="tok"')


@pytest.mark.parametrize("name", ["Гостиная", "客厅", "Küche", "Kodi+ (p1d)"])
def test_a_device_name_in_any_script_is_a_latin1_header_jellyfin_decodes(name):
    """http.client refuses a header value Latin-1 cannot encode, and
    Jellyfin URL-decodes every value, ``+`` as a space included."""
    header = auth.build_auth_header(name, "d", "1", token="tok")

    header.encode("latin-1")  # what http.client does with it
    value = header.split('Device="', 1)[1].split('"', 1)[0]
    assert unquote_plus(value) == name


def test_auth_result_parses_response():
    result = auth.AuthResult.from_response(
        {
            "AccessToken": "tok",
            "ServerId": "srv",
            "User": {"Id": "uid", "Name": "conor"},
        }
    )
    assert (result.token, result.server_id, result.user_id, result.user_name) == (
        "tok",
        "srv",
        "uid",
        "conor",
    )


def test_auth_result_tolerates_missing_fields():
    result = auth.AuthResult.from_response({})
    assert result.token == "" and result.user_id == ""
