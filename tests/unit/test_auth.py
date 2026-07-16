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
    ],
)
def test_normalize_address(raw, expected):
    assert auth.normalize_address(raw) == expected


def test_auth_header_without_token():
    header = auth.build_auth_header("Living Room", "dev123", "0.1.0")
    assert header == (
        'MediaBrowser Client="Kofin", Device="Living Room", '
        'DeviceId="dev123", Version="0.1.0"'
    )


def test_auth_header_with_token_and_quote_escape():
    header = auth.build_auth_header('The "Box"', "d", "1", token="tok")
    assert "Device=\"The 'Box'\"" in header
    assert header.endswith('Token="tok"')


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
