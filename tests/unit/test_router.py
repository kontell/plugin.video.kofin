"""Router dispatch: how a plugin:// invocation picks its handler."""

import subprocess
import sys
from pathlib import Path

import pytest

from kofin.plugin import router


@pytest.fixture
def routed(monkeypatch):
    """Replace the route resolver with recorders for every registered mode."""
    seen = []

    def recorder(mode):
        def handler(request):
            seen.append((mode, request))

        return handler

    table = {mode: recorder(mode) for mode in router.ROUTES}
    monkeypatch.setattr(router, "_resolve", table.get)
    monkeypatch.setattr(router, "_root", recorder("<root>"))
    return seen


def argv(query, handle="-1"):
    return ["plugin://plugin.video.kofin/", handle, query]


def test_dispatch_routes_by_mode(routed):
    router.dispatch(argv("?mode=syncplay"))
    assert [mode for mode, _ in routed] == ["syncplay"]


def test_dispatch_tolerates_a_trailing_slash_on_the_mode(routed):
    """A library node's <path> is a folder path, so it gets written with a
    trailing slash. Without this the mode reads as "syncplay/", no handler
    matches, and the route silently serves the root listing instead."""
    router.dispatch(argv("?mode=syncplay/"))
    router.dispatch(argv("?mode=adduser/"))

    assert [mode for mode, _ in routed] == ["syncplay", "adduser"]


def test_dispatch_falls_back_to_root_on_an_unknown_mode(routed):
    router.dispatch(argv("?mode=nosuchmode"))
    assert [mode for mode, _ in routed] == ["<root>"]


def test_dispatch_with_no_mode_is_the_root_route(routed):
    """The empty mode is a registered handler, not the unknown-mode fallback."""
    router.dispatch(argv(""))
    assert [mode for mode, _ in routed] == [""]


def test_dispatch_reads_handle_and_resume(routed):
    router.dispatch(
        ["plugin://plugin.video.kofin/", "7", "?mode=syncplay&id=abc", "resume:true"]
    )

    _, request = routed[0]
    assert request.handle == 7
    assert request.resume is True
    assert request.params["id"] == "abc"


def test_dispatch_keeps_the_unslashed_mode_in_params(routed):
    """Only the handler lookup is normalised; params stay as Kodi sent them."""
    router.dispatch(argv("?mode=syncplay/"))

    _, request = routed[0]
    assert request.params["mode"] == "syncplay/"


def test_every_route_resolves_to_a_callable():
    """ROUTES holds dotted names resolved at dispatch (so a click imports only
    its own handler); a typo in either part would otherwise surface only when
    that mode is first hit on a real box."""
    for mode in router.ROUTES:
        assert callable(router._resolve(mode)), mode


def test_importing_the_router_imports_no_handler_modules():
    """The lazy table is the point: on builds that never reuse the language
    invoker, whatever the router pulls in is paid again on every click
    (docs/perf-hardening-plan.md W1.2). Checked in a subprocess so this
    test's own imports cannot mask a regression."""
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import kofin.plugin.router; "
        "handlers = [m for m in sys.modules if m.startswith('kofin.plugin.') "
        "and m != 'kofin.plugin.router']; "
        "assert not handlers, handlers; "
        "assert 'requests' not in sys.modules, 'requests imported at dispatch load'"
        % lib
    )
    subprocess.check_call([sys.executable, "-c", code])
