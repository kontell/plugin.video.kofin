"""Route plugin invocations (pluginsource and RunPlugin) to handlers."""

from importlib import import_module
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl

from kofin.core.log import Logger

LOG = Logger(__name__)

Params = Dict[str, str]


class Request:
    def __init__(
        self,
        base_url: str,
        handle: int,
        params: Params,
        resume: bool = False,
    ) -> None:
        self.base_url = base_url
        self.handle = handle
        self.params = params
        self.resume = resume


# The handler registry, as (module under kofin.plugin, attribute) pairs
# resolved per dispatch. Dotted names rather than imported callables because a
# plugin invocation must only pay for the module it routes to: a cold
# invocation re-imports from scratch, and importing all ten handler modules
# pulled in the whole requests tree — ~1 s inside Kodi's Python — for routes
# that never touch the network (docs/perf-hardening-plan.md W1.2). Invoker
# reuse spares a warm click that work, but only while nothing else on the
# system runs Python in between, so the lazy table still earns its keep.
ROUTES: Dict[str, Tuple[str, str]] = {
    "": ("browse", "root"),
    "streams": ("streams", "menu"),
    "browse": ("browse", "browse"),
    "continuewatching": ("browse", "continue_watching"),
    "nextepisodes": ("browse", "next_episodes"),
    "extras": ("browse", "extras"),
    "lyrics": ("lyrics", "lyrics"),
    "play": ("play", "play"),
    "syncplay": ("syncplay", "menu"),
    "login": ("account", "login"),
    "logout": ("account", "logout"),
    "cleandatabases": ("clean", "clean_databases"),
    "testconnection": ("account", "test_connection"),
    "restart": ("account", "restart"),
    "settings": ("actions", "open_settings"),
    "adduser": ("adduser", "who_is_watching"),
    "whoshortlist": ("adduser", "select_shortlist"),
    "userprefs": ("userprefs", "jellyfin_settings"),
    "watched": ("actions", "watched"),
    "unwatched": ("actions", "unwatched"),
    "download": ("actions", "download"),
    "downloadshow": ("actions", "download_show"),
    "downloadshows": ("actions", "manage_download_shows"),
    "canceldownload": ("actions", "cancel_download"),
    "removedownload": ("actions", "remove_download"),
    "deletealldownloads": ("actions", "delete_all_downloads"),
    "favorite": ("actions", "favorite"),
    "unfavorite": ("actions", "unfavorite"),
    "delete": ("actions", "delete_item"),
    "selectlibraries": ("librarypicker", "select_libraries"),
    "updatelibs": ("actions", "update_libraries"),
    "repairlibs": ("actions", "repair_libraries"),
    "refreshboxsets": ("actions", "refresh_boxsets"),
    "precacheart": ("actions", "precache_art"),
}

# The routes that answer a directory fetch themselves: they either build a
# listing and call endOfDirectory (browse and friends, lyrics) or resolve a
# playable item with setResolvedUrl (play, on every path including its
# failures). Every other route runs for its side effect and returns nothing,
# and dispatch closes the handle on its behalf.
#
# That backstop is load-bearing, not tidiness. With reuselanguageinvoker the
# invoker thread parks between invocations instead of exiting, and Kodi only
# marks a script done from CLanguageInvokerThread::OnExit — so
# CScriptRunner::WaitOnScriptResult, which loops on IsRunning(scriptId) with
# no timeout on its first loop, waits forever for a route that leaves its
# handle open. Before the flag was honoured the interpreter died after every
# invocation and the fetch failed out at once, which is exactly what the
# fire-an-IPC-and-exit routes (syncplay, adduser) were written against and
# what their docstrings still describe. Measured: mode=streams reached as a
# directory finished its script in 2 ms and hung the caller indefinitely.
# test_router.py refuses any ROUTES entry that is in neither set.
LISTING_MODES = frozenset(
    {"", "browse", "continuewatching", "nextepisodes", "extras", "lyrics", "play"}
)


def _resolve(mode: str) -> Optional[Callable[[Request], None]]:
    """The handler for ``mode``, imported on demand; None when unregistered."""
    route = ROUTES.get(mode)
    if route is None:
        return None
    module = import_module("kofin.plugin." + route[0])
    handler: Callable[[Request], None] = getattr(module, route[1])
    return handler


def _root(request: Request) -> None:
    """Unknown-mode fallback: the root listing."""
    from kofin.plugin import browse

    browse.root(request)


def dispatch(argv: List[str]) -> None:
    base_url = argv[0] if argv else ""
    handle = -1
    if len(argv) > 1:
        try:
            handle = int(argv[1])
        except ValueError:
            handle = -1
    query = argv[2] if len(argv) > 2 else ""
    params = dict(parse_qsl(query.lstrip("?")))
    # Kodi appends "resume:true|false" for video plugin items (the native
    # resume prompt's outcome).
    resume = len(argv) > 3 and argv[3].split(":", 1)[-1] == "true"
    request = Request(base_url, handle, params, resume)

    # A library node's <path> is a folder path, so it is natural (and, for
    # some Kodi paths, automatic) to write it with a trailing slash — which
    # lands here as mode="syncplay/" and would silently fall back to the root
    # listing instead of running the route.
    mode = params.get("mode", "").rstrip("/")
    handler = _resolve(mode)
    LOG.debug("dispatch mode=%s params=%s handle=%s", mode or "<root>", params, handle)
    builds_listing = mode in LISTING_MODES
    if handler is None:
        LOG.warning("unknown mode %r; showing root", mode)
        handler = _root
        # The fallback *is* the root listing, whatever was asked for.
        builds_listing = True
    handler(request)

    # Close the handle for the routes that build nothing, so a directory fetch
    # that reached one fails out at once instead of waiting on an invoker
    # thread that parks rather than exiting (see LISTING_MODES).
    if handle >= 0 and not builds_listing:
        import xbmcplugin

        xbmcplugin.endOfDirectory(handle, succeeded=False)
