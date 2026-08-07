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
# plugin invocation must only pay for the module it routes to: on builds where
# the language invoker is never reused (this repo's primary test box, see
# docs/perf-hardening-plan.md W1.2), every invocation re-imports from scratch,
# and importing all ten handler modules pulled in the whole requests tree —
# ~1 s inside Kodi's Python — for routes that never touch the network.
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
    "watched": ("actions", "watched"),
    "unwatched": ("actions", "unwatched"),
    "favorite": ("actions", "favorite"),
    "unfavorite": ("actions", "unfavorite"),
    "delete": ("actions", "delete_item"),
    "selectlibraries": ("librarypicker", "select_libraries"),
    "updatelibs": ("actions", "update_libraries"),
    "repairlibs": ("actions", "repair_libraries"),
    "refreshboxsets": ("actions", "refresh_boxsets"),
    "precacheart": ("actions", "precache_art"),
}


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
    if handler is None:
        LOG.warning("unknown mode %r; showing root", mode)
        handler = _root
    handler(request)
