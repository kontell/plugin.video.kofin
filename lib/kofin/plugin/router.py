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
    "search": ("browse", "search"),
    "lyrics": ("lyrics", "lyrics"),
    "play": ("play", "play"),
    "syncplay": ("syncplay", "menu"),
    "findservers": ("serverpicker", "find_servers"),
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
    "resetresume": ("actions", "reset_resume"),
    "playall": ("playall", "play_all"),
    "download": ("actions", "download"),
    "downloadshow": ("actions", "download_show"),
    "downloadshows": ("actions", "manage_download_shows"),
    "canceldownload": ("actions", "cancel_download"),
    "removedownload": ("actions", "remove_download"),
    "deletealldownloads": ("actions", "delete_all_downloads"),
    "downloadsize": ("actions", "download_size"),
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
    {
        "",
        "browse",
        "continuewatching",
        "nextepisodes",
        "extras",
        "search",
        "lyrics",
        "play",
    }
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
    # One trailing slash off the whole query before it is parsed. A library
    # node's <path> is a folder path and carries one, and it lands on whichever
    # parameter happens to be last — which is only ``mode`` for the routes that
    # take no other. Stripping it after the parse covered those and nothing
    # else: measured on Omega 21.3, a node on
    # ?mode=browse&view=…&type=movies&folder=all/ reached browse with
    # folder="all/", which node_query matches nowhere, so the listing fell
    # through to its container branch and asked the server for an item with
    # that id — a 400, a failed fetch, and a node that reads as broken beside a
    # ?mode=continuewatching/ one that works.
    params = dict(parse_qsl(query.lstrip("?").rstrip("/")))
    # Kodi appends "resume:true|false" for video plugin items (the native
    # resume prompt's outcome).
    resume = len(argv) > 3 and argv[3].split(":", 1)[-1] == "true"
    request = Request(base_url, handle, params, resume)

    # Already unslashed by the query strip above, which is where a node path's
    # trailing slash is answered for every parameter rather than just this one.
    mode = params.get("mode", "")
    handler = _resolve(mode)
    LOG.debug("dispatch mode=%s params=%s handle=%s", mode or "<root>", params, handle)
    builds_listing = mode in LISTING_MODES
    if handler is None:
        LOG.warning("unknown mode %r; showing root", mode)
        handler = _root
        # The fallback *is* the root listing, whatever was asked for.
        builds_listing = True
    failed = False
    try:
        handler(request)
    except BaseException:
        failed = True
        raise
    finally:
        # Close the handle for the routes that build nothing, so a directory
        # fetch that reached one fails out at once instead of waiting on an
        # invoker thread that parks rather than exiting (see LISTING_MODES) —
        # and for *any* route whose handler raised. A listing route catches
        # JellyfinError and closes its own handle on that path; everything
        # else (a locked kofin.db under the play resolve, a settings read
        # that failed) used to leave the handle open, which is the same
        # indefinite wait. The exception still propagates, so Kodi logs the
        # traceback exactly as before; only the caller is released.
        if handle >= 0 and (failed or not builds_listing):
            import xbmcplugin

            xbmcplugin.endOfDirectory(handle, succeeded=False)
