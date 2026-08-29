"""A minimal admin client for the jf12 test server, for host-side probes.

Reads ``JF12_HOST``/``JF12_PORT`` from ``../test-server/jf12.env`` and the
admin credential (``JF12_ADMIN_USER``/``JF12_ADMIN_PW``) from
``~/.config/kodi-drive/targets.env``; prints neither. Only the ``Authorization:
MediaBrowser ..., Token=`` form is sent — the legacy headers are rejected on
v12 (test-server README).

The jf12 rule applies to every caller: nothing is ever deleted through the
Items API and no file is written under a library path. A *user* this module
creates for a probe is the caller's to delete again.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional, Tuple

ENV_FILES = (
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "test-server", "jf12.env"
    ),
    "~/.config/kodi-drive/targets.env",
)


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for path in ENV_FILES:
        try:
            with open(os.path.expanduser(path)) as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            continue
    return env


class Jf12:
    def __init__(self) -> None:
        env = load_env()
        host = env.get("JF12_HOST") or "127.0.0.1"
        port = env.get("JF12_PORT") or "8098"
        self.base = "http://%s:%s" % (host, port)
        self._admin_user = env.get("JF12_ADMIN_USER", "admin")
        self._admin_pw = env.get("JF12_ADMIN_PW", "")
        self._auth = (
            'MediaBrowser Client="kofin-probe", Device="kofin-probe", '
            'DeviceId="probe-%s", Version="0"' % uuid.uuid4().hex[:8]
        )
        self.admin_token: Optional[str] = None

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        token: Optional[str] = None,
    ) -> Tuple[int, str, Any]:
        """(status, content-type, parsed JSON or raw bytes)."""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, method=method, data=data)
        request.add_header("Content-Type", "application/json")
        request.add_header(
            "Authorization", self._auth + (', Token="%s"' % token if token else "")
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                return response.status, content_type, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                parsed: Any = json.loads(raw) if raw else None
            except ValueError:
                parsed = raw[:200]
            return error.code, error.headers.get("Content-Type", ""), parsed

    def login_admin(self) -> str:
        if not self._admin_pw:
            raise SystemExit("no JF12_ADMIN_PW in targets.env")
        status, _, body = self.call(
            "POST",
            "/Users/AuthenticateByName",
            {"Username": self._admin_user, "Pw": self._admin_pw},
        )
        if status != 200:
            raise SystemExit("admin login failed: %s" % status)
        self.admin_token = body["AccessToken"]
        return self.admin_token

    def create_user(self, name: str, password: str) -> Dict[str, Any]:
        status, _, user = self.call(
            "POST", "/Users/New", {"Name": name, "Password": password}, self.admin_token
        )
        if status != 200:
            raise SystemExit("user create failed: %s %s" % (status, user))
        return user

    def set_policy(self, user_id: str, policy: Dict[str, Any]) -> int:
        status, _, _ = self.call(
            "POST", "/Users/%s/Policy" % user_id, policy, self.admin_token
        )
        return status

    def delete_user(self, user_id: str) -> int:
        status, _, _ = self.call(
            "DELETE", "/Users/%s" % user_id, None, self.admin_token
        )
        return status

    def login(self, name: str, password: str) -> str:
        status, _, body = self.call(
            "POST", "/Users/AuthenticateByName", {"Username": name, "Pw": password}
        )
        if status != 200:
            raise SystemExit("login as %s failed: %s" % (name, status))
        return body["AccessToken"]


def shape(body: Any) -> Any:
    """A printable summary of a listing: counts for lists, values otherwise."""
    if isinstance(body, dict):
        return {k: (len(v) if isinstance(v, list) else v) for k, v in body.items()}
    if isinstance(body, list):
        return "list[%d]" % len(body)
    return body
