"""Router dispatch: how a plugin:// invocation picks its handler."""

import subprocess
import sys
from pathlib import Path

import pytest
import xbmcplugin

from kofin.plugin import router

# Every route that does not build a listing, spelled out so that adding one to
# ROUTES forces a decision here. dispatch closes the handle for exactly these:
# leave a listing route out of LISTING_MODES and its listing is replaced by a
# failed fetch; put an action route into it and a directory fetch that reaches
# the route hangs forever under invoker reuse (see router.LISTING_MODES).
ACTION_MODES = {
    "streams",
    "syncplay",
    "login",
    "logout",
    "cleandatabases",
    "testconnection",
    "restart",
    "settings",
    "adduser",
    "whoshortlist",
    "userprefs",
    "watched",
    "unwatched",
    "resetresume",
    "playall",
    "download",
    "downloadshow",
    "downloadshows",
    "canceldownload",
    "removedownload",
    "deletealldownloads",
    "favorite",
    "unfavorite",
    "delete",
    "selectlibraries",
    "updatelibs",
    "repairlibs",
    "refreshboxsets",
    "precacheart",
}


@pytest.fixture
def ended(monkeypatch):
    """Record endOfDirectory calls the router makes on a handler's behalf."""
    calls = []
    monkeypatch.setattr(
        xbmcplugin,
        "endOfDirectory",
        lambda handle, succeeded=True, **kw: calls.append((handle, succeeded)),
    )
    return calls


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


def test_dispatch_tolerates_a_trailing_slash_on_the_last_parameter(routed):
    """The slash lands on whichever parameter is last, not on the mode — so
    answering it on the mode alone only ever covered the routes that take no
    other parameter. Verified on Omega 21.3: a node whose <path> was
    ?mode=browse&view=…&type=movies&folder=all/ reached browse with
    folder="all/", node_query matched no branch, and the listing asked the
    server for a container with that id. The server answered 400, the route
    failed the fetch, and the node read as broken while the single-parameter
    ?mode=continuewatching/ node beside it worked."""
    router.dispatch(argv("?mode=browse&view=abc&type=movies&folder=all/"))

    mode, request = routed[0]
    assert mode == "browse"
    assert request.params["folder"] == "all"
    assert request.params["view"] == "abc"
    assert request.params["type"] == "movies"


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


def test_dispatch_unslashes_the_params_it_hands_the_handler(routed):
    """The strip happens before the parse, so the handler is handed the value
    Kodi meant rather than the folder path's spelling of it. Handlers compare
    these against server ids and node keys; nothing downstream re-strips."""
    router.dispatch(argv("?mode=syncplay/"))

    _, request = routed[0]
    assert request.params["mode"] == "syncplay"


def test_every_route_resolves_to_a_callable():
    """ROUTES holds dotted names resolved at dispatch (so a click imports only
    its own handler); a typo in either part would otherwise surface only when
    that mode is first hit on a real box."""
    for mode in router.ROUTES:
        assert callable(router._resolve(mode)), mode


def test_every_route_is_classified():
    """dispatch closes the handle for every mode outside LISTING_MODES, so a
    new route has to be classified deliberately rather than by omission."""
    assert router.LISTING_MODES <= set(router.ROUTES)
    assert set(router.ROUTES) - router.LISTING_MODES == ACTION_MODES


def test_an_action_route_has_its_handle_closed(routed, ended):
    """Under reuselanguageinvoker the invoker thread parks instead of exiting,
    and Kodi marks a script done only when the thread exits — so a route that
    leaves its handle open makes CScriptRunner::WaitOnScriptResult wait on
    IsRunning() forever, on a first loop that has no timeout. Measured live:
    mode=streams reached as a directory ran in 2 ms and hung its caller
    indefinitely. Closing the handle restores the fail-fast the IPC routes
    (syncplay, adduser) were written against."""
    router.dispatch(argv("?mode=syncplay", handle="7"))

    assert ended == [(7, False)]


def test_a_listing_route_closes_its_own_handle(routed, ended):
    """browse and friends call endOfDirectory themselves; a second call here
    would overwrite a good listing with a failed fetch."""
    router.dispatch(argv("?mode=browse", handle="7"))

    assert ended == []


def test_the_unknown_mode_fallback_is_treated_as_a_listing(routed, ended):
    """The fallback runs the root listing whatever was asked for, so the
    handle is already answered by the time dispatch returns."""
    router.dispatch(argv("?mode=nosuchmode", handle="7"))

    assert ended == []


def test_a_handleless_invocation_closes_nothing(routed, ended):
    """RunPlugin and the context items dispatch with handle -1: there is no
    directory fetch waiting, and endOfDirectory on -1 is meaningless."""
    router.dispatch(argv("?mode=syncplay", handle="-1"))

    assert ended == []


def test_importing_the_router_imports_no_handler_modules():
    """The lazy table is the point: on a cold invocation whatever the router
    pulls in is paid again, and invoker reuse only spares the clicks that
    happen to keep the thread (docs/perf-hardening-plan.md W1.2, W4.4).
    Checked in a subprocess so this test's own imports cannot mask a
    regression."""
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


# --- a handler that raises must still release the caller ---------------------


def _raising(mode_to_raise):
    def resolve(mode):
        if mode == mode_to_raise:

            def boom(request):
                raise RuntimeError("database is locked")

            return boom
        return None

    return resolve


def test_a_listing_handler_that_raises_still_closes_its_handle(ended, monkeypatch):
    """Every listing route catches only JellyfinError and closes its own
    handle on that path; anything else escaped with the handle open, and
    under invoker reuse an open handle is a caller that waits forever
    (assessment P1). The exception still propagates."""
    monkeypatch.setattr(router, "_resolve", _raising("browse"))
    with pytest.raises(RuntimeError):
        router.dispatch(argv("?mode=browse", "7"))
    assert ended == [(7, False)]


def test_a_play_handler_that_raises_still_closes_its_handle(ended, monkeypatch):
    monkeypatch.setattr(router, "_resolve", _raising("play"))
    with pytest.raises(RuntimeError):
        router.dispatch(argv("?mode=play&id=x", "7"))
    assert ended == [(7, False)]


def test_an_action_handler_that_raises_closes_its_handle_once(ended, monkeypatch):
    monkeypatch.setattr(router, "_resolve", _raising("watched"))
    with pytest.raises(RuntimeError):
        router.dispatch(argv("?mode=watched&id=x", "7"))
    assert ended == [(7, False)]


def test_a_raise_on_a_negative_handle_closes_nothing(ended, monkeypatch):
    monkeypatch.setattr(router, "_resolve", _raising("browse"))
    with pytest.raises(RuntimeError):
        router.dispatch(argv("?mode=browse", "-1"))
    assert ended == []


def test_a_listing_handler_that_returns_is_left_to_close_itself(
    routed, ended, monkeypatch
):
    router.dispatch(argv("?mode=browse", "7"))
    assert ended == []  # the recorder never called it, and neither did dispatch
