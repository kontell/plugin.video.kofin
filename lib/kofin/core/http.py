"""HTTP transport: one persistent session per process, retries, error taxonomy.

Pure python (no Kodi imports) so the whole network stack is unit-testable.

``requests`` is imported inside the methods that use it, not at module load:
importing this module must stay free. The requests tree costs ~1 s inside
Kodi's Python (no bytecode cache), and routes that never talk to the server —
a node menu, a settings button — import this module through the Api plumbing
all the same (docs/perf-hardening-plan.md W1.2). Invoker reuse amortises that
cost when it happens, but it is opportunistic — Kodi keeps one reusable thread
for the whole system, so anything else running Python between two clicks sends
the next one back to a cold interpreter.
"""

import random
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, Optional, Tuple, Type

from kofin.core.log import Logger

if TYPE_CHECKING:  # runtime import is deferred; see module docstring
    import requests

LOG = Logger(__name__)

DEFAULT_TIMEOUT = (6.0, 30.0)
RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5

# One read per chunk of a streamed body. Large enough that the per-chunk
# abort check costs nothing against a media download, small enough that a
# stop request is heard within a fraction of a second on any real link.
STREAM_CHUNK_BYTES = 262_144

# Default retry budget per method, applied when the caller passes none. GETs
# replay safely. A DELETE states an absolute fact (unfavorite, mark unplayed,
# close this transcode) and gets one replay. POST gets none: the transport
# cannot tell "never arrived" from "response lost after the server acted", and
# a replayed POST double-applies — a second SyncPlay group, a queue item added
# twice, a group advanced two items, a duplicate playback-history row, a
# second AutoOpenLiveStream transcode session nothing ever closes. A caller
# whose POST is an absolute-state write may opt back in with an explicit
# ``retries``.
METHOD_RETRIES = {"GET": RETRIES, "HEAD": RETRIES, "DELETE": 1}

# Answers that mean "not now" rather than "no": a reverse proxy holding the
# door while Jellyfin restarts (502/504), a server still warming up (503), a
# rate limiter (429). They ride the same ladder as a transport error, for
# the methods that carry a budget — POST still gets none (audit F7). Not
# 500: Jellyfin answers deterministic 500s for broken items, and replaying
# those would only slow a walk by three backoffs per request.
RETRY_STATUSES = (429, 502, 503, 504)


class JellyfinError(Exception):
    """Base for all transport/API failures."""


class ServerUnreachable(JellyfinError):
    pass


class Unauthorized(JellyfinError):
    pass


class HttpError(JellyfinError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def run_ladder(
    method: str,
    url: str,
    retries: int,
    attempt: Callable[[], Any],
    transport_errors: Tuple[Type[BaseException], ...],
    abort: Optional[Callable[[], bool]] = None,
) -> Any:
    """The one retry ladder both transports ride (P1.9): backoff, the
    abort check between attempts, the per-request log line, and the
    status taxonomy.

    The abort check is measured, not assumed: a black-holed GET rides the
    full ladder — 4 attempts x (6s connect + 30s read) plus backoff, about
    147s, none of which would consult the stop flag. Kodi will not finalise
    a script while a thread it started is alive, so it sits on "waiting on
    thread <id>" for exactly that long and every later Python invocation
    queues behind it. Giving up here bounds the damage at the one read
    already in flight.
    """
    last_error: Optional[BaseException] = None
    for attempt_index in range(retries + 1):
        if attempt_index:
            if abort is not None and abort():
                raise ServerUnreachable(
                    "%s %s: abandoned while stopping (%s)" % (method, url, last_error)
                )
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt_index - 1))
            time.sleep(delay + random.uniform(0, delay / 2))
            # sleep() cannot see the flag; a stop that lands during backoff
            # used to start another full-timeout GET. Check again.
            if abort is not None and abort():
                raise ServerUnreachable(
                    "%s %s: abandoned while stopping (%s)" % (method, url, last_error)
                )
        try:
            response = attempt()
        except transport_errors as error:
            LOG.debug(
                "attempt %d/%d failed for %s: %s",
                attempt_index + 1,
                retries + 1,
                url,
                error,
            )
            last_error = error
            continue

        # Every request, not just the failures: the scenario gates assert
        # request *counts* ("zero per-show /Episodes calls", "3067 fetches
        # to 0"), and those are ungreppable if only errors are logged.
        # Debug level, and masked like every other line — kofin's auth
        # rides in headers, so the query string carries no secret.
        sent = getattr(response, "request", None)
        LOG.debug(
            "http %s %s -> %d",
            method,
            getattr(sent, "url", None) or getattr(response, "url", None) or url,
            response.status_code,
        )

        if response.status_code in (401, 403):
            raise Unauthorized("%s %s -> %d" % (method, url, response.status_code))
        if response.status_code in RETRY_STATUSES and attempt_index < retries:
            LOG.debug(
                "attempt %d/%d answered %d for %s; replaying",
                attempt_index + 1,
                retries + 1,
                response.status_code,
                url,
            )
            last_error = HttpError(
                response.status_code,
                "%s %s -> %d" % (method, url, response.status_code),
            )
            continue
        if 300 <= response.status_code < 400:
            # A redirect is refused on both transports rather than followed
            # by one of them (audit F4): requests followed it silently while
            # the stdlib plugin transport handed the empty 302 body back as
            # an empty library — a working service beside a plugin listing
            # nothing. Nobody had a working redirected address, so refusing
            # costs no one and names the cause at login: the Location is
            # the address the user should have entered.
            location = (getattr(response, "headers", None) or {}).get("Location")
            raise HttpError(
                response.status_code,
                "%s %s -> %d (redirected to %s; use that address)"
                % (method, url, response.status_code, location or "?"),
            )
        if response.status_code >= 400:
            raise HttpError(
                response.status_code,
                "%s %s -> %d" % (method, url, response.status_code),
            )
        return response

    raise ServerUnreachable("%s %s: %s" % (method, url, last_error))


def plugin_transport(verify_ssl: bool = True) -> "Http":
    """The transport for the *plugin* process.

    Standard library rather than ``requests``, because a plugin invocation is
    short-lived and never reuses its interpreter: importing requests costs
    ~1 s inside Kodi's Python on both test boxes and is paid on every click
    that talks to the server. The service keeps the requests path
    (kofin/core/stdhttp.py explains the split).

    Imported here rather than at module load so this module stays free of
    both trees until something actually asks for one.
    """
    from kofin.core.stdhttp import StdlibHttp

    return StdlibHttp(verify_ssl)


class StreamedResponse:
    """A streaming GET in flight: headers now, the body drained via chunks().

    Thin on purpose — retries, resume bookkeeping and file writes are the
    download manager's policy, not the transport's. What lives here is what
    every streaming caller needs identically: the abort check between chunks
    (the read timeout bounds a dead socket; this bounds a *live* one, which a
    multi-GB body otherwise keeps reading long past a stop request), the
    mid-body error taxonomy, and close() so an abandoned stream releases its
    pooled connection.
    """

    def __init__(
        self,
        response: "requests.Response",
        abort: Optional[Callable[[], bool]],
        already_complete: bool = False,
    ) -> None:
        self._response = response
        self._abort = abort
        # The server answered 416 to a resume offset: the range starts at or
        # past the end, i.e. everything was already fetched (feasibility V1;
        # jellyfin-android reads the same answer the same way).
        self.already_complete = already_complete

    @property
    def status(self) -> int:
        return int(self._response.status_code)

    def header(self, name: str) -> str:
        """A response header, '' when absent. Case-insensitive by hand so a
        test fake with a plain dict behaves like requests' own mapping."""
        for key, value in self._response.headers.items():
            if key.lower() == name.lower():
                return str(value)
        return ""

    def chunks(self) -> Iterator[bytes]:
        import requests

        if self.already_complete:
            return
        try:
            for chunk in self._response.iter_content(STREAM_CHUNK_BYTES):
                if self._abort is not None and self._abort():
                    raise ServerUnreachable("stream abandoned while stopping")
                if chunk:
                    yield chunk
        except requests.RequestException as error:
            # A body that dies mid-read is a connection fact, whatever
            # requests dresses it as (ChunkedEncodingError and friends).
            raise ServerUnreachable("stream interrupted: %s" % error) from error

    def close(self) -> None:
        try:
            self._response.close()
        except Exception as error:  # pragma: no cover - defensive
            LOG.debug("stream close failed: %s", error)


class Http:
    """A lazily created, kept-alive requests session."""

    def __init__(
        self, verify_ssl: bool = True, abort: Optional[Callable[[], bool]] = None
    ) -> None:
        self._verify_ssl = verify_ssl
        # Asked between retries: True means give up rather than replay. Passed
        # in rather than read here so this module keeps its no-Kodi-imports
        # property; the service wires its stop flag in (service/main.py).
        self._abort = abort
        self._session: Optional["requests.Session"] = None

    def session(self) -> "requests.Session":
        import requests

        if self._session is None:
            session = requests.Session()
            session.verify = self._verify_ssl
            if not self._verify_ssl:
                # The user turned verification off deliberately (a private CA,
                # a self-signed box); urllib3 would otherwise write a warning
                # into Kodi's log for every single request made.
                try:
                    import urllib3

                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                except Exception:  # pragma: no cover - defensive
                    pass
            self._session = session
            LOG.debug("http session opened (verify_ssl=%s)", self._verify_ssl)
        return self._session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception as error:  # pragma: no cover - defensive
                LOG.warning("session close failed: %s", error)
            self._session = None

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[Tuple[float, float]] = None,
        retries: Optional[int] = None,
    ) -> "requests.Response":
        import requests

        if retries is None:
            retries = METHOD_RETRIES.get(method.upper(), 0)

        def attempt() -> "requests.Response":
            # Redirects reach run_ladder's 3xx refusal like they do on the
            # stdlib transport, instead of being followed here alone.
            return self.session().request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout or DEFAULT_TIMEOUT,
                allow_redirects=False,
            )

        response: "requests.Response" = run_ladder(
            method,
            url,
            retries,
            attempt,
            (requests.ConnectionError, requests.Timeout),
            abort=self._abort,
        )
        return response

    def stream(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        start: int = 0,
        timeout: Optional[Tuple[float, float]] = None,
    ) -> StreamedResponse:
        """One streaming GET — deliberately no retry ladder.

        The download manager owns retry and resume policy: a replayed
        multi-GB body is never the transport's call to make, and the
        manager's resume watermark decides where the next attempt starts.
        ``start`` resumes with a Range header (server support verified,
        feasibility V1); a 416 answer comes back as ``already_complete``
        rather than an error, because for a resume it means exactly that.
        """
        import requests

        sent = dict(headers or {})
        if start:
            sent["Range"] = "bytes=%d-" % start
        try:
            response = self.session().request(
                "GET",
                url,
                headers=sent,
                stream=True,
                timeout=timeout or DEFAULT_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            raise ServerUnreachable("GET %s: %s" % (url, error)) from error
        LOG.debug("http GET %s -> %d (stream)", url, response.status_code)
        if response.status_code == 416:
            response.close()
            return StreamedResponse(response, self._abort, already_complete=True)
        if response.status_code in (401, 403):
            response.close()
            raise Unauthorized("GET %s -> %d" % (url, response.status_code))
        if response.status_code >= 400:
            response.close()
            raise HttpError(
                response.status_code, "GET %s -> %d" % (url, response.status_code)
            )
        return StreamedResponse(response, self._abort)
