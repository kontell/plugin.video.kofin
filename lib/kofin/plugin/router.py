"""Route plugin invocations (pluginsource and RunPlugin) to handlers."""

from typing import Callable, Dict, List
from urllib.parse import parse_qsl

from kofin.core.log import Logger

LOG = Logger(__name__)

Params = Dict[str, str]


class Request:
    def __init__(self, base_url: str, handle: int, params: Params) -> None:
        self.base_url = base_url
        self.handle = handle
        self.params = params


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
    request = Request(base_url, handle, params)

    mode = params.get("mode", "")
    handler = _handlers().get(mode)
    LOG.debug("dispatch mode=%s params=%s handle=%s", mode or "<root>", params, handle)
    if handler is None:
        LOG.warning("unknown mode %r; showing root", mode)
        handler = _root
    handler(request)


def _root(request: Request) -> None:
    from kofin.plugin import browse

    browse.root(request)


def _handlers() -> Dict[str, Callable[[Request], None]]:
    # Imports deferred so a plugin invocation only pays for what it routes to.
    from kofin.plugin import browse

    return {
        "": _root,
        "browse": browse.browse,
    }
