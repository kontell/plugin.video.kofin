"""Router dispatch: how a plugin:// invocation picks its handler."""

import pytest

from kofin.plugin import router


@pytest.fixture
def routed(monkeypatch):
    """Replace the handler table with recorders for every registered mode."""
    seen = []

    def recorder(mode):
        def handler(request):
            seen.append((mode, request))

        return handler

    table = {mode: recorder(mode) for mode in router._handlers()}
    monkeypatch.setattr(router, "_handlers", lambda: table)
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
