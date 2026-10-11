"""The gate in front of the router: at most two builds of one listing at a
time, decided before anything else of kofin's is imported."""

import subprocess
import sys
from pathlib import Path

import pytest
import xbmcplugin

from kofin.core import state
from kofin.plugin import gate, router
from tests.unit.fakes import FakeWindow

ROOT = Path(__file__).resolve().parents[2]
BASE = "plugin://plugin.video.kofin/"


@pytest.fixture(autouse=True)
def window(monkeypatch):
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    return FakeWindow


@pytest.fixture
def ended(monkeypatch):
    calls = []
    monkeypatch.setattr(
        xbmcplugin,
        "endOfDirectory",
        lambda handle, succeeded=True, **kw: calls.append((handle, succeeded)),
    )
    return calls


def _slots():
    return [k for k in FakeWindow.store if k.startswith(gate.PROP)]


def test_the_property_is_the_one_state_declares():
    """core/state.py is the registry of what kofin shares live; the gate
    spells the name itself only because importing that module is what it
    exists to avoid."""
    assert gate.PROP == state.PROP_LISTING_FLIGHT


def test_a_url_is_built_twice_at_once_and_the_third_request_is_turned_away():
    """Kodi asks for a home widget again at every visit to Home, whether or
    not the last request has answered. Nothing bounded them, and on a
    Raspberry Pi 3 thirty requests for the add-on's root were waiting each
    time Kodi aborted for want of a thread."""
    url = BASE + "?"
    first, second = gate.claim(url), gate.claim(url)
    assert first and second and first != second
    assert gate.claim(url) is None
    # Another listing is another matter.
    other = gate.claim(BASE + "?mode=continuewatching")
    assert other
    gate.leave(first)
    third = gate.claim(url)
    assert third and gate.claim(url) is None
    for token in (second, third, other):
        gate.leave(token)
    assert _slots() == []


def test_a_slot_whose_request_died_is_free_at_its_deadline(monkeypatch):
    """Slots, not a counter: a request Kodi killed never gives its slot
    back, and a count left one too high would turn listings away for ever."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock["now"])
    assert gate.claim(BASE) and gate.claim(BASE)
    assert gate.claim(BASE) is None
    clock["now"] += gate.SECONDS - 1
    assert gate.claim(BASE) is None
    clock["now"] += 2
    assert gate.claim(BASE)


def test_a_slot_that_passed_to_another_request_is_not_given_back_by_the_first(
    monkeypatch,
):
    clock = {"now": 1000.0}
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock["now"])
    stale = gate.claim(BASE)
    clock["now"] += gate.SECONDS + 1
    fresh = gate.claim(BASE)
    key, _, mine = fresh.partition("\n")
    assert key == stale.partition("\n")[0]
    gate.leave(stale)
    assert FakeWindow.store[key] == mine
    gate.leave(fresh)
    assert key not in FakeWindow.store


def test_garbage_in_a_slot_reads_as_free():
    key = gate.claim(BASE).partition("\n")[0]
    FakeWindow.store[key] = "not-a-deadline"
    assert gate.claim(BASE).partition("\n")[0] == key


def test_a_window_that_keeps_nothing_bounds_nothing(monkeypatch):
    """Refusing every listing would be the worse failure: a window whose
    properties do not read back admits the request, with nothing to give
    back afterwards."""

    class Forgetful(FakeWindow):
        def getProperty(self, key):
            return ""

    monkeypatch.setattr("xbmcgui.Window", Forgetful)
    for _ in range(5):
        token = gate.claim(BASE)
        assert token == ""
        gate.leave(token)


def test_only_a_home_screen_listing_is_counted():
    """A play resolve, the cleaner's existence check, an action, a search
    and the scanner's own listings hold no slot and are never turned away:
    a native listing refused is a directory Kodi takes for empty."""
    native = BASE + "native/%s/%s/movies/" % ("a" * 32, "b" * 32)
    assert gate.bounded([BASE, "7", ""]) == BASE + "?"
    assert gate.bounded([BASE, "7", "?mode=browse&view=a&type=movies&folder=all/"]) == (
        BASE + "?mode=browse&view=a&type=movies&folder=all"
    )
    assert gate.bounded([BASE, "7", "?mode=continuewatching/"]) == (
        BASE + "?mode=continuewatching"
    )
    for argv in (
        [BASE, "7", "?mode=play&id=x"],
        [BASE, "7", "?mode=search"],
        [BASE, "7", "?mode=lyrics&id=x"],
        [BASE, "7", "?mode=watched&id=x"],
        [BASE, "7", "?mode=browse&kodi_action=check_exists"],
        [BASE, "-1", "?mode=browse"],
        [BASE, "x", ""],
        [BASE],
        [native, "7", ""],
        [native + "x/", "7", "?mode=play&id=x"],
    ):
        assert gate.bounded(argv) == "", argv
    # Every counted route builds its own listing, and none is one that must
    # always answer.
    assert gate.BOUNDED_MODES <= router.LISTING_MODES
    assert not gate.BOUNDED_MODES & {"play", "check_exists"}


def test_a_request_turned_away_has_its_directory_ended_and_nothing_else(ended):
    url = BASE + "?mode=nextepisodes"
    held = [gate.claim(url), gate.claim(url)]
    assert gate.enter([BASE, "7", "?mode=nextepisodes"]) is None
    assert ended == [(7, False)]
    # What is not counted goes through with nothing held, whatever is full.
    assert gate.enter([BASE, "8", "?mode=play&id=x"]) == ""
    gate.leave(held[0])
    token = gate.enter([BASE, "9", "?mode=nextepisodes/"])
    assert token and ended == [(7, False)]
    gate.leave(token)
    gate.leave(held[1])
    assert _slots() == []


def _run_default(monkeypatch, argv, handler):
    """default.py as Kodi runs it: argv[0] is the plugin URL. (runpy would
    put the script's path there.)"""
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(router, "_resolve", lambda mode: handler)
    path = str(ROOT / "default.py")
    with open(path, encoding="utf-8") as source:
        exec(compile(source.read(), path, "exec"), {"__file__": path})


def test_the_entry_script_holds_a_slot_for_as_long_as_the_listing_is_built(
    ended, monkeypatch
):
    seen = []

    def handler(request):
        seen.append((request.handle, len(_slots())))
        # The same listing asked for twice more while this one is built.
        if len(seen) < 3:
            _run_default(monkeypatch, [BASE, str(request.handle + 1), ""], handler)

    _run_default(monkeypatch, [BASE, "7", ""], handler)
    assert seen == [(7, 1), (8, 2)]
    assert ended == [(9, False)]
    assert _slots() == []


def test_the_entry_script_gives_the_slot_back_when_the_listing_fails(
    ended, monkeypatch
):
    def boom(request):
        raise RuntimeError("database is locked")

    for _ in range(4):
        with pytest.raises(RuntimeError):
            _run_default(monkeypatch, [BASE, "7", "?mode=browse&view=a"], boom)
    assert _slots() == []


def test_a_request_turned_away_has_imported_none_of_the_rest_of_kofin():
    """The point of deciding in default.py: turned away after the router's
    imports, a request still took forty seconds on a loaded Raspberry Pi 3
    and Kodi aborted all the same. Checked in a subprocess so this test's
    own imports cannot mask a regression."""
    code = (
        "import sys, xbmcgui\n"
        "store = {}\n"
        "class Window:\n"
        "    def __init__(self, window_id=0): pass\n"
        "    def getProperty(self, key): return store.get(key, '')\n"
        "    def setProperty(self, key, value): store[key] = value\n"
        "    def clearProperty(self, key): store.pop(key, None)\n"
        "xbmcgui.Window = Window\n"
        "sys.path.insert(0, %r)\n"
        "from kofin.plugin import gate\n"
        "url = 'plugin://plugin.video.kofin/?'\n"
        "assert gate.claim(url) and gate.claim(url)\n"
        "sys.argv = ['plugin://plugin.video.kofin/', '5', '']\n"
        "path = %r\n"
        "exec(compile(open(path).read(), path, 'exec'), {'__file__': path})\n"
        "assert len(store) == 2, store\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('kofin'))\n"
        "assert loaded == ['kofin', 'kofin.plugin', 'kofin.plugin.gate'], loaded\n"
    ) % (str(ROOT / "lib"), str(ROOT / "default.py"))
    subprocess.check_call([sys.executable, "-c", code])
