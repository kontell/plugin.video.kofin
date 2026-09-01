"""L1: the Account-tab LAN server picker (?mode=findservers).

The scan and the HTTP probe are both stubbed — ``test_discovery.py`` owns the
protocol. What is pinned here is the route's contract: which address ends up
in ``serverAddress``, and that nothing is written on any path the user backed
out of.
"""

import time

import pytest

from kofin.core import discovery
from kofin.core.http import ServerUnreachable
from kofin.plugin import serverpicker
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon, FakeDialog

MINIPIE = discovery.Found(
    server_id="id-minipie",
    name="minipie",
    address="http://192.168.1.167:8096",
    source_host="192.168.1.167",
)
# Publishes an external hostname to a caller on its own wire — the case the
# verification exists for.
ATTIC = discovery.Found(
    server_id="id-attic",
    name="attic",
    address="https://attic.example.com:8920",
    source_host="10.0.0.5",
)


class FakeProgress:
    def __init__(self):
        self.created = []
        self.updates = []
        self.closed = False
        self.closed_at = None
        self.cancel = False

    def create(self, heading, message=""):
        self.created.append((heading, message))

    def update(self, percent, message=""):
        self.updates.append((percent, message))

    def iscanceled(self):
        return self.cancel

    def close(self):
        if not self.closed:
            self.closed_at = time.monotonic()
        self.closed = True


class FakeMonitor:
    def waitForAbort(self, _seconds):
        return False


class FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Rig:
    """Everything the route touches, with the knobs the tests turn."""

    def __init__(self, dialog, progress, transport):
        self.dialog = dialog
        self.progress = progress
        self.transport = transport
        self.found = []
        self.reachable = {}
        self.probed = []
        self.builtins = []
        self.logged_in = False
        self.scan_delay = 0.0
        self.scan_ended_at = None

    def scan(self, on_found=None, **_kwargs):
        if self.scan_delay:
            time.sleep(self.scan_delay)
        for entry in self.found:
            if on_found is not None:
                on_found(entry)
        self.scan_ended_at = time.monotonic()
        return list(self.found)

    def probe(self, _transport, address):
        self.probed.append(address)
        if address not in self.reachable:
            raise ServerUnreachable("no route to %s" % address)
        return self.reachable[address]


@pytest.fixture
def rig(monkeypatch):
    FakeAddon.store = {}
    dialog = FakeDialog()
    progress = FakeProgress()
    transport = FakeTransport()
    rig = Rig(dialog, progress, transport)

    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr(serverpicker.xbmcgui, "Dialog", lambda: dialog)
    monkeypatch.setattr(serverpicker.xbmcgui, "DialogProgress", lambda: progress)
    monkeypatch.setattr(serverpicker.xbmc, "Monitor", FakeMonitor)
    monkeypatch.setattr(
        serverpicker.xbmc,
        "executebuiltin",
        lambda command: rig.builtins.append(command),
    )
    monkeypatch.setattr(serverpicker, "plugin_transport", lambda *_a, **_k: transport)
    monkeypatch.setattr(serverpicker.discovery, "scan", rig.scan)
    monkeypatch.setattr(serverpicker.auth, "probe_public_info", rig.probe)
    monkeypatch.setattr(serverpicker.settings, "localized", _localized)
    monkeypatch.setattr(
        serverpicker.Credentials,
        "load",
        classmethod(lambda cls: _creds(rig.logged_in)),
    )
    return rig


def _localized(string_id):
    """Mirrors the real strings' format specifiers.

    #30831 takes the server's name; the shared FakeAddon answers every id
    without one, and ``"" % name`` is a TypeError rather than a no-op.
    """
    return "L%d %%s" % string_id if string_id == 30831 else "L%d" % string_id


def _creds(logged_in):
    creds = serverpicker.Credentials(device_id="d")
    creds.is_logged_in = logged_in
    return creds


def _request():
    return Request("plugin://plugin.video.kofin/", -1, {"mode": "findservers"})


def _stored():
    return FakeAddon.store.get("serverAddress")


# -- the happy path -----------------------------------------------------------


def test_the_chosen_address_lands_in_the_setting(rig):
    rig.found = [MINIPIE]
    rig.reachable = {MINIPIE.address: {"ServerName": "minipie", "Version": "10.11.0"}}
    rig.dialog.select_answers = [0]

    serverpicker.find_servers(_request())

    assert _stored() == "http://192.168.1.167:8096"


def test_the_settings_dialog_is_reopened_so_the_field_is_the_confirmation(rig):
    rig.found = [MINIPIE]
    rig.reachable = {MINIPIE.address: {}}
    rig.dialog.select_answers = [0]

    serverpicker.find_servers(_request())

    assert rig.builtins == ["Addon.OpenSettings(plugin.video.kofin)"]


def test_the_transport_is_closed_whatever_happens(rig):
    rig.found = []

    serverpicker.find_servers(_request())

    assert rig.transport.closed


def test_the_progress_dialog_is_closed(rig):
    rig.found = [MINIPIE]
    rig.reachable = {MINIPIE.address: {}}
    rig.dialog.select_answers = [0]

    serverpicker.find_servers(_request())

    assert rig.progress.closed


# -- verification -------------------------------------------------------------


def test_the_published_address_is_tried_first(rig):
    rig.found = [ATTIC]
    rig.reachable = {ATTIC.address: {}}
    rig.dialog.select_answers = [0]

    serverpicker.find_servers(_request())

    assert rig.probed[0] == "https://attic.example.com:8920"
    assert _stored() == "https://attic.example.com:8920"


def test_an_unreachable_published_url_falls_back_to_the_source_address(rig):
    """The address a server publishes is derived per caller and can name a
    host this box cannot reach; the datagram's own source can."""
    rig.found = [ATTIC]
    rig.reachable = {"https://10.0.0.5:8920": {"ServerName": "attic"}}
    rig.dialog.select_answers = [0]

    serverpicker.find_servers(_request())

    assert rig.probed == ["https://attic.example.com:8920", "https://10.0.0.5:8920"]
    assert _stored() == "https://10.0.0.5:8920"


def test_a_server_that_answered_nothing_is_offered_last_not_hidden(rig):
    """'attic' sorts first by name and last by reachability. The user may know
    the network better than the probe does, so it stays on the list."""
    rig.found = [ATTIC, MINIPIE]
    rig.reachable = {MINIPIE.address: {}}
    rig.dialog.select_answers = [0]

    serverpicker.find_servers(_request())

    _heading, choices, _preselect = rig.dialog.selects[0]
    assert len(choices) == 2
    assert _stored() == MINIPIE.address


def test_picking_an_unreachable_server_writes_it_but_warns(rig):
    rig.found = [ATTIC, MINIPIE]
    rig.reachable = {MINIPIE.address: {}}
    rig.dialog.select_answers = [1]  # the unreachable one, sorted last

    serverpicker.find_servers(_request())

    assert _stored() == ATTIC.address
    assert rig.dialog.notifications == ["L30831 attic"]


# -- the paths that must not write --------------------------------------------


def test_cancelling_the_picker_writes_nothing(rig):
    rig.found = [MINIPIE]
    rig.reachable = {MINIPIE.address: {}}
    rig.dialog.select_answers = []  # FakeDialog answers -1 once exhausted

    serverpicker.find_servers(_request())

    assert _stored() is None
    assert rig.builtins == []


def test_cancelling_the_scan_stops_before_the_picker(rig):
    """The window is short, but its Cancel has to answer: the scan runs on a
    worker precisely so this thread can keep the dialog honest."""
    rig.found = [MINIPIE]
    rig.reachable = {MINIPIE.address: {}}
    rig.dialog.select_answers = [0]
    rig.scan_delay = 0.05  # long enough for the poll loop to see the cancel
    rig.progress.cancel = True

    serverpicker.find_servers(_request())

    assert rig.dialog.selects == []
    assert _stored() is None
    # The point of running the scan on a worker: the dialog goes at Cancel,
    # not when the window it can no longer stop finally closes.
    assert rig.progress.closed_at < rig.scan_ended_at


def test_nothing_found_warns_and_offers_no_picker(rig):
    rig.found = []

    serverpicker.find_servers(_request())

    assert rig.dialog.notifications == ["L30829"]
    assert rig.dialog.selects == []
    assert _stored() is None


def test_a_signed_in_user_is_not_scanned_at_all(rig):
    """The button is hidden once signed in, but the route is reachable by URL
    and serverAddress is the live server's."""
    rig.logged_in = True
    rig.found = [MINIPIE]

    serverpicker.find_servers(_request())

    assert rig.probed == []
    assert rig.dialog.selects == []
    assert _stored() is None
