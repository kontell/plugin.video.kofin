"""L1 units for 'Who's watching?' and its Advanced-tab shortlist: which users
the toggle dialog offers, what the picker records, and session restore."""

import pytest

from kofin.core import state
from kofin.core.http import JellyfinError
from kofin.plugin import adduser
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon, FakeWindow

USERS = [
    {"Id": "primary", "Name": "Alice"},
    {"Id": "u2", "Name": "Bob"},
    {"Id": "u3", "Name": "Carol"},
    {"Id": "u4", "Name": "Dan"},
]


def _names(users):
    return [user["Name"] for user in users]


def test_offerable_drops_the_primary_user():
    """The session's own user cannot be added or removed, so it is never a row."""
    assert _names(adduser.offerable(USERS, "primary", [], set())) == [
        "Bob",
        "Carol",
        "Dan",
    ]


def test_offerable_without_a_shortlist_offers_everyone_else():
    assert len(adduser.offerable(USERS, "primary", [], set())) == 3


def test_offerable_honours_the_shortlist():
    picked = adduser.offerable(USERS, "primary", ["u2", "u4"], set())
    assert _names(picked) == ["Bob", "Dan"]


def test_offerable_keeps_users_already_on_the_session():
    """Dropping someone from the shortlist while they are still on the session
    must not strand them there: the toggle list is also the only way off."""
    picked = adduser.offerable(USERS, "primary", ["u2"], {"u3"})
    assert _names(picked) == ["Bob", "Carol"]


# --- restore filter ----------------------------------------------------------


def test_users_to_restore_skips_those_already_on_the_session():
    assert adduser.users_to_restore(["a", "b", "c"], {"a", "c"}) == ["b"]


def test_users_to_restore_preserves_desired_order_and_dedupes():
    assert adduser.users_to_restore(["b", "a", "b", ""], {"x"}) == ["b", "a"]


def test_users_to_restore_empty_desired_is_empty():
    assert adduser.users_to_restore([], {"a"}) == []


# --- the shortlist picker ----------------------------------------------------


class FakeDialog:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def multiselect(self, heading, choices, **kwargs):
        self.calls.append((heading, list(choices), kwargs.get("preselect")))
        return self.result

    def notification(self, *args, **kwargs):
        pass


class UsersApi:
    user_id = "primary"

    def users(self):
        return USERS


@pytest.fixture
def picker(monkeypatch):
    FakeAddon.store = {}
    dialog = FakeDialog()
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr(adduser.xbmcgui, "Dialog", lambda: dialog)
    monkeypatch.setattr(
        adduser.Api, "from_credentials", staticmethod(lambda *a, **k: UsersApi())
    )
    monkeypatch.setattr(
        adduser.Credentials, "load", classmethod(lambda cls: _logged_in())
    )
    return dialog


def _logged_in():
    creds = adduser.Credentials(user_id="primary", token="t", device_id="d")
    creds.is_logged_in = True
    return creds


def _request(mode="whoshortlist"):
    return Request("plugin://plugin.video.kofin/", -1, {"mode": mode})


def test_shortlist_records_ids_not_names(picker):
    """A rename on the server must not silently empty the shortlist."""
    picker.result = [0, 2]  # Bob, Dan — the primary user is not a row

    adduser.select_shortlist(_request())

    assert FakeAddon.store["whoIsWatchingShortlist"] == "u2,u4"


def test_shortlist_preselects_the_current_choice(picker):
    FakeAddon.store["whoIsWatchingShortlist"] = "u3"
    picker.result = None

    adduser.select_shortlist(_request())

    _, choices, preselect = picker.calls[0]
    assert choices == ["Bob", "Carol", "Dan"]
    assert preselect == [1]


def test_shortlist_cancelled_changes_nothing(picker):
    FakeAddon.store["whoIsWatchingShortlist"] = "u3"
    picker.result = None

    adduser.select_shortlist(_request())

    assert FakeAddon.store["whoIsWatchingShortlist"] == "u3"


def test_shortlist_emptied_means_everyone(picker):
    FakeAddon.store["whoIsWatchingShortlist"] = "u3"
    picker.result = []

    adduser.select_shortlist(_request())

    assert FakeAddon.store["whoIsWatchingShortlist"] == ""


# --- the Who's watching? toggle (live session + durable setting) -------------


class SessionApi(UsersApi):
    def __init__(self, additional=None):
        self.additional = list(additional or [])
        self.added = []
        self.removed = []

    def device_sessions(self, device_id):
        return [
            {
                "Id": "sess1",
                "AdditionalUsers": [
                    {"UserId": uid, "UserName": uid} for uid in self.additional
                ],
            }
        ]

    def session_add_user(self, session_id, user_id):
        self.added.append((session_id, user_id))
        self.additional.append(user_id)

    def session_remove_user(self, session_id, user_id):
        self.removed.append((session_id, user_id))
        self.additional = [uid for uid in self.additional if uid != user_id]


@pytest.fixture
def toggle(monkeypatch):
    FakeAddon.store = {}
    dialog = FakeDialog()
    api = SessionApi()
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr(adduser.xbmcgui, "Dialog", lambda: dialog)
    monkeypatch.setattr(adduser.xbmc, "executebuiltin", lambda *_a: None)
    monkeypatch.setattr(
        adduser.Api, "from_credentials", staticmethod(lambda *a, **k: api)
    )
    monkeypatch.setattr(
        adduser.Credentials, "load", classmethod(lambda cls: _logged_in())
    )
    # Real string is "Who's watching with %s?"; FakeAddon has no placeholders.
    monkeypatch.setattr(adduser.settings, "localized", lambda sid: "with %s")
    return dialog, api


def test_toggle_persists_chosen_ids(toggle):
    """Confirming the dialog is what makes the selection survive a restart."""
    dialog, api = toggle
    dialog.result = [0, 2]  # Bob, Dan

    adduser.show_picker(api, _logged_in())

    assert FakeAddon.store["whoIsWatching"] == "u2,u4"
    assert api.added == [("sess1", "u2"), ("sess1", "u4")]


def test_toggle_publishes_the_new_names_before_the_refresh(toggle, monkeypatch):
    """A confirmed change republishes server truth so the root redraw reads
    the new set from the property, not a stale one."""
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    dialog, api = toggle
    dialog.result = [0, 2]  # -> u2, u4

    adduser.show_picker(api, _logged_in())

    assert state.watching_names() == ["u2", "u4"]


def test_toggle_cleared_persists_nobody(toggle):
    dialog, api = toggle
    api.additional = ["u3"]
    FakeAddon.store["whoIsWatching"] = "u3"
    dialog.result = []

    adduser.show_picker(api, _logged_in())

    assert FakeAddon.store["whoIsWatching"] == ""
    assert api.removed == [("sess1", "u3")]


def test_toggle_cancelled_changes_nothing(toggle):
    dialog, api = toggle
    FakeAddon.store["whoIsWatching"] = "u2"
    dialog.result = None

    adduser.show_picker(api, _logged_in())

    assert FakeAddon.store["whoIsWatching"] == "u2"
    assert api.added == []
    assert api.removed == []


def test_toggle_persists_even_when_session_api_fails(toggle, monkeypatch):
    """Intended set is saved first so the next session can retry a failed add."""
    dialog, api = toggle
    dialog.result = [0]

    def boom(session_id, user_id):
        raise JellyfinError("server busy")

    api.session_add_user = boom

    adduser.show_picker(api, _logged_in())

    assert FakeAddon.store["whoIsWatching"] == "u2"


# --- the route handler (service hand-off) ------------------------------------


@pytest.fixture
def route(monkeypatch):
    FakeAddon.store = {}
    sent = []
    dialog = FakeDialog()
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr(adduser.xbmcgui, "Dialog", lambda: dialog)
    monkeypatch.setattr(
        adduser.ipc, "notify", lambda method, data=None: sent.append(method)
    )
    monkeypatch.setattr(
        adduser.Credentials, "load", classmethod(lambda cls: _logged_in())
    )
    monkeypatch.setattr(adduser.state, "is_online", lambda: True)
    return sent, dialog


def test_route_hands_off_to_the_service(route):
    """The dialog must not run here: a plugin invocation that blocks on a modal
    cannot be reached as a library node (Kodi runs the node <path> as a
    directory fetch and the two fight)."""
    sent, dialog = route

    adduser.who_is_watching(_request("adduser"))

    assert sent == [adduser.ipc.WHO_IS_WATCHING]
    assert dialog.calls == []


def test_route_is_quiet_when_logged_out(route, monkeypatch):
    sent, _ = route
    creds = adduser.Credentials(user_id="", token="", device_id="d")
    creds.is_logged_in = False
    monkeypatch.setattr(adduser.Credentials, "load", classmethod(lambda cls: creds))

    adduser.who_is_watching(_request("adduser"))

    assert sent == []


def test_route_does_not_notify_when_offline(route, monkeypatch):
    sent, _ = route
    monkeypatch.setattr(adduser.state, "is_online", lambda: False)

    adduser.who_is_watching(_request("adduser"))

    assert sent == []


# --- restore_additional_users ------------------------------------------------


def test_restore_adds_missing_users_only(monkeypatch):
    FakeAddon.store = {"whoIsWatching": "u2,u3"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    api = SessionApi(additional=["u2"])
    api.user_id = "primary"

    adduser.restore_additional_users(api, "d")

    assert api.added == [("sess1", "u3")]


def test_restore_empty_setting_adds_nobody(monkeypatch):
    """No saved set means no adds; the session is still read once so the
    root-label property reflects whatever is already on it."""
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    api = SessionApi()

    adduser.restore_additional_users(api, "d")

    assert api.added == []


def test_restore_publishes_the_names_for_the_root_label(monkeypatch):
    """The connect-time restore feeds state.PROP_WHO_NAMES; the root listing
    renders its label from that property alone, with no /Sessions round trip
    per render (perf plan W1.4)."""
    FakeAddon.store = {"whoIsWatching": "u2"}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    api = SessionApi()
    api.user_id = "primary"

    adduser.restore_additional_users(api, "d")

    assert api.added == [("sess1", "u2")]
    assert state.watching_names() == ["u2"]


def test_restore_publishes_session_truth_even_with_nothing_saved(monkeypatch):
    """A set attached elsewhere (another client, the dashboard) must still
    reach the label — which is why the session is read before the saved-set
    check rather than after."""
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    api = SessionApi(additional=["u9"])

    adduser.restore_additional_users(api, "d")

    assert api.added == []
    assert state.watching_names() == ["u9"]


def test_restore_skips_primary_user(monkeypatch):
    FakeAddon.store = {"whoIsWatching": "primary,u2"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    api = SessionApi()
    api.user_id = "primary"

    adduser.restore_additional_users(api, "d")

    assert api.added == [("sess1", "u2")]


def test_restore_survives_a_failed_add(monkeypatch):
    FakeAddon.store = {"whoIsWatching": "u2,u3"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    api = SessionApi()
    api.user_id = "primary"
    calls = []

    def flaky(session_id, user_id):
        calls.append(user_id)
        if user_id == "u2":
            raise JellyfinError("gone")
        api.additional.append(user_id)

    api.session_add_user = flaky

    adduser.restore_additional_users(api, "d")

    assert calls == ["u2", "u3"]


def test_restore_no_session_yet_is_quiet(monkeypatch):
    FakeAddon.store = {"whoIsWatching": "u2"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    api = SessionApi()
    api.device_sessions = lambda device_id: []

    adduser.restore_additional_users(api, "d")  # must not raise
