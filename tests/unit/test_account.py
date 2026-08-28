import pytest

from kofin.core import auth, settings
from kofin.core.http import HttpError, ServerUnreachable, Unauthorized
from kofin.plugin import account
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon


class FakeDialog:
    inputs = []
    selects = []
    yesno_answer = True
    notifications = []

    def input(self, heading, defaultt="", type=0, option=0):
        return FakeDialog.inputs.pop(0) if FakeDialog.inputs else ""

    def select(self, heading, options):
        return FakeDialog.selects.pop(0) if FakeDialog.selects else -1

    def yesno(self, heading, message):
        return FakeDialog.yesno_answer

    def notification(self, heading, message, icon="", time=0, sound=True):
        FakeDialog.notifications.append(message)


class FakeProgress:
    def create(self, heading, message=""):
        pass

    def update(self, percent, message=""):
        pass

    def iscanceled(self):
        return False

    def close(self):
        pass


class FakeMonitor:
    def waitForAbort(self, seconds=0):
        return False


# Strings used with %-formatting need placeholder-correct fakes.
TEXTS = {
    30015: "code %s",
    30016: "in as %s",
    30019: "out from %s?",
    30021: "ok %s %s",
    30027: "sign in to %s",
    30821: "server error %s",
}


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeDialog.inputs = []
    FakeDialog.selects = []
    FakeDialog.yesno_answer = True
    FakeDialog.notifications = []
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Dialog", FakeDialog)
    monkeypatch.setattr("xbmcgui.DialogProgress", FakeProgress)
    monkeypatch.setattr("xbmc.Monitor", FakeMonitor)
    monkeypatch.setattr(account, "_text", lambda sid: TEXTS.get(sid, "msg"))
    sent = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: sent.append(cmd))
    return sent


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(
        account.auth, "public_info", lambda h, a: {"ServerName": "minipie"}
    )
    monkeypatch.setattr(
        account.auth,
        "authenticate_password",
        lambda h, a, hdr, u, p: auth.AuthResult("tok9", "uid9", u, "srv9"),
    )


REQ = Request("plugin://plugin.video.kofin/", -1, {})


def test_login_password_happy_path(kodi_fakes, server, monkeypatch):
    monkeypatch.setattr(account.auth, "quick_connect_enabled", lambda h, a, hdr: False)
    settings.set_str("serverAddress", "minipie")
    FakeDialog.inputs = ["conor", "secretpw"]

    account.login(REQ)

    creds = settings.Credentials.load()
    assert creds.is_logged_in is True
    assert creds.token == "tok9"
    assert creds.server_address == "http://minipie:8096"
    assert creds.server_name == "minipie"
    assert creds.display_user == "conor"
    assert any("AuthChanged" in cmd for cmd in kodi_fakes)


def test_login_quick_connect_path(kodi_fakes, server, monkeypatch):
    monkeypatch.setattr(account.auth, "quick_connect_enabled", lambda h, a, hdr: True)
    monkeypatch.setattr(
        account.auth,
        "quick_connect_initiate",
        lambda h, a, hdr: {"Secret": "s1", "Code": "ABC123"},
    )
    monkeypatch.setattr(account.auth, "quick_connect_poll", lambda h, a, hdr, s: True)
    monkeypatch.setattr(
        account.auth,
        "authenticate_quick_connect",
        lambda h, a, hdr, s: auth.AuthResult("tokQC", "uidQC", "conor", "srv9"),
    )
    settings.set_str("serverAddress", "minipie")
    FakeDialog.selects = [0]

    account.login(REQ)

    creds = settings.Credentials.load()
    assert creds.token == "tokQC"
    assert creds.is_logged_in is True


def test_login_wrong_password_leaves_logged_out(kodi_fakes, monkeypatch):
    monkeypatch.setattr(
        account.auth, "public_info", lambda h, a: {"ServerName": "minipie"}
    )
    monkeypatch.setattr(account.auth, "quick_connect_enabled", lambda h, a, hdr: False)

    def raise_unauthorized(h, a, hdr, u, p):
        raise Unauthorized("401")

    monkeypatch.setattr(account.auth, "authenticate_password", raise_unauthorized)
    settings.set_str("serverAddress", "minipie")
    FakeDialog.inputs = ["conor", "wrong"]

    account.login(REQ)

    creds = settings.Credentials.load()
    assert creds.is_logged_in is False
    assert creds.token == ""
    assert not any("AuthChanged" in cmd for cmd in kodi_fakes)


def test_logout_clears_and_notifies(kodi_fakes, monkeypatch):
    creds = settings.Credentials.load()
    creds.server_address = "http://minipie:8096"
    creds.server_name = "minipie"
    creds.token = "tok9"
    creds.user_id = "uid9"
    creds.is_logged_in = True
    creds.save()
    FakeAddon.store["whoIsWatching"] = "u2,u4"
    FakeAddon.store["whoIsWatchingShortlist"] = "u2,u3"
    called = []
    monkeypatch.setattr(account.auth, "logout", lambda h, a, hdr: called.append(a))

    account.logout(REQ)

    assert called == ["http://minipie:8096"]
    loaded = settings.Credentials.load()
    assert loaded.is_logged_in is False
    assert loaded.token == ""
    assert loaded.server_address == "http://minipie:8096"
    assert FakeAddon.store.get("whoIsWatching", "") == ""
    assert FakeAddon.store["whoIsWatchingShortlist"] == "u2,u3"
    assert any("AuthChanged" in cmd for cmd in kodi_fakes)


def _logged_in():
    creds = settings.Credentials.load()
    creds.server_address = "http://minipie:8096"
    creds.token = "tok9"
    creds.user_id = "uid9"
    creds.is_logged_in = True
    creds.save()


class _Transport:
    closed = False

    def close(self):
        _Transport.closed = True


def _api_raising(error, monkeypatch):
    class FakeApi:
        @classmethod
        def from_credentials(cls, transport, creds, interactive=False):
            return cls()

        def public_info(self):
            raise error

        def views(self):
            return []

    monkeypatch.setattr(account, "plugin_transport", lambda verify: _Transport())
    monkeypatch.setattr("kofin.core.api.Api", FakeApi)


def test_connection_reports_a_server_error_instead_of_raising(kodi_fakes, monkeypatch):
    """A 5xx is neither "unreachable" nor "session rejected": the server
    answered, badly. Before, it was an uncaught HttpError out of a settings
    button (assessment P2)."""
    _logged_in()
    _api_raising(HttpError(500, "HTTP 500 for /System/Info/Public"), monkeypatch)
    _Transport.closed = False

    account.test_connection(REQ)

    assert (
        FakeDialog.notifications[-1] == "server error HTTP 500 for /System/Info/Public"
    )
    assert _Transport.closed is True


def test_connection_still_names_an_unreachable_server(kodi_fakes, monkeypatch):
    _logged_in()
    _api_raising(ServerUnreachable("refused"), monkeypatch)

    account.test_connection(REQ)

    assert FakeDialog.notifications[-1] == "msg"  # #30018 via the stub texts
