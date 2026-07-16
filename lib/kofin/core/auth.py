"""Server resolution and authentication flows (dialog-free, unit-testable).

The UI around these calls (keyboards, Quick Connect code dialog) lives in
kofin.plugin.account; this module only talks to the server.
"""

from typing import Any, Dict
from urllib.parse import urlsplit

from kofin.core.http import Http
from kofin.core.log import Logger

LOG = Logger(__name__)

DEFAULT_HTTP_PORT = 8096


class AuthResult:
    def __init__(
        self, token: str, user_id: str, user_name: str, server_id: str
    ) -> None:
        self.token = token
        self.user_id = user_id
        self.user_name = user_name
        self.server_id = server_id

    @classmethod
    def from_response(cls, data: Dict[str, Any]) -> "AuthResult":
        user = data.get("User") or {}
        return cls(
            token=data.get("AccessToken", ""),
            user_id=user.get("Id", ""),
            user_name=user.get("Name", ""),
            server_id=data.get("ServerId", ""),
        )


def normalize_address(text: str) -> str:
    """'host', 'host:port' or a full URL -> canonical base URL, no trailing /.

    A bare host gets http on port 8096; explicit schemes and ports pass
    through (https without a port stays portless, i.e. 443).
    """
    address = text.strip().rstrip("/")
    if not address:
        return ""
    if "://" not in address:
        address = "http://" + address
    parts = urlsplit(address)
    netloc = parts.netloc
    if ":" not in netloc and parts.scheme == "http":
        netloc = "%s:%d" % (netloc, DEFAULT_HTTP_PORT)
    base = "%s://%s" % (parts.scheme, netloc)
    if parts.path:
        base += parts.path.rstrip("/")
    return base


def build_auth_header(
    device_name: str, device_id: str, version: str, token: str = ""
) -> str:
    fields = [
        'MediaBrowser Client="Kofin"',
        'Device="%s"' % device_name.replace('"', "'"),
        'DeviceId="%s"' % device_id,
        'Version="%s"' % version,
    ]
    if token:
        fields.append('Token="%s"' % token)
    return ", ".join(fields)


def public_info(http: Http, address: str) -> Dict[str, Any]:
    response = http.request("GET", address + "/System/Info/Public", retries=1)
    info: Dict[str, Any] = response.json()
    return info


def quick_connect_enabled(http: Http, address: str, header: str) -> bool:
    response = http.request(
        "GET",
        address + "/QuickConnect/Enabled",
        headers={"Authorization": header},
        retries=1,
    )
    return bool(response.json())


def quick_connect_initiate(http: Http, address: str, header: str) -> Dict[str, Any]:
    response = http.request(
        "POST",
        address + "/QuickConnect/Initiate",
        headers={"Authorization": header},
        retries=1,
    )
    state: Dict[str, Any] = response.json()
    return state


def quick_connect_poll(http: Http, address: str, header: str, secret: str) -> bool:
    response = http.request(
        "GET",
        address + "/QuickConnect/Connect",
        headers={"Authorization": header},
        params={"secret": secret},
        retries=1,
    )
    state = response.json()
    return bool(state.get("Authenticated"))


def authenticate_quick_connect(
    http: Http, address: str, header: str, secret: str
) -> AuthResult:
    response = http.request(
        "POST",
        address + "/Users/AuthenticateWithQuickConnect",
        headers={"Authorization": header},
        json_body={"Secret": secret},
        retries=1,
    )
    return AuthResult.from_response(response.json())


def authenticate_password(
    http: Http, address: str, header: str, username: str, password: str
) -> AuthResult:
    response = http.request(
        "POST",
        address + "/Users/AuthenticateByName",
        headers={"Authorization": header},
        json_body={"Username": username, "Pw": password},
        retries=1,
    )
    return AuthResult.from_response(response.json())


def logout(http: Http, address: str, header: str) -> None:
    try:
        http.request(
            "POST",
            address + "/Sessions/Logout",
            headers={"Authorization": header},
            retries=0,
        )
    except Exception as error:
        # Best effort: local credentials are cleared regardless.
        LOG.warning("server-side logout failed: %s", error)
