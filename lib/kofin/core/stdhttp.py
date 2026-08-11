"""The same HTTP contract as :mod:`kofin.core.http`, over the standard library.

Why this exists is one measurement. Inside Kodi's Python, on both test boxes::

    import requests    1.11 s (Omega/Debian)   0.93 s (Kodi 22/Android)
    http.client + ssl  0.00 s                  0.00 s   (already loaded)

The plugin process is short-lived, so that second is paid on every cold click
that talks to the server — a browse listing, the play resolve, a context
action. Kodi 22's own bytecode caching does not help: the cost is executing the
package's module bodies, not compiling them. Invoker reuse (W4.4) removes the
import from a *warm* click, but it cannot be relied on: Kodi keeps a single
reusable invoker thread for the whole system, so any other addon's script
between two clicks discards ours and the next click pays full price again.

So the plugin process asks over the standard library, and the service keeps
``requests``: a long-lived process pays the import once, its sync stack is
proven against it, and none of this is worth a second transport there.

What this is not: a general HTTP client. It implements exactly the surface
:class:`kofin.core.api.Api` uses, with the same error taxonomy and the same
per-method retry budget, and is tested against the same contract. Anything
subtler than that — redirects, streamed bodies, content decoding — is either
unused on kofin's API paths or deliberately out of scope (see the notes on
``Accept-Encoding`` and redirects below).
"""

import http.client
import json
import random
import ssl
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlsplit

from kofin.core.http import (
    BACKOFF_BASE_SECONDS,
    DEFAULT_TIMEOUT,
    METHOD_RETRIES,
    Http,
    HttpError,
    ServerUnreachable,
    Unauthorized,
)
from kofin.core.log import Logger

LOG = Logger(__name__)

# Errors that mean "the exchange failed", as opposed to "the server answered
# something we dislike". OSError covers socket timeouts, refusals and resets;
# HTTPException covers the protocol-level failures http.client raises.
_TRANSPORT_ERRORS = (http.client.HTTPException, OSError)


class Response:
    """The three things ``Api`` reads off a response.

    Deliberately not a requests.Response lookalike beyond that: pretending to
    be one would invite code to depend on the rest of that surface, which this
    module has no intention of providing.
    """

    def __init__(self, status_code: int, content: bytes, url: str) -> None:
        self.status_code = status_code
        self.content = content
        self.url = url

    def json(self) -> Any:
        return json.loads(self.content)


class StdlibHttp(Http):
    """A kept-alive connection to one origin, over ``http.client``.

    Subclasses :class:`Http` so it can be handed to ``Api`` unchanged; the two
    methods that touch the network are overridden and the inherited
    ``session()`` is never reached (it is the requests path).

    One connection per thread, reused across that thread's calls: the play
    route makes three or four, and paying a TLS handshake for each would give
    back a good part of what the cheap import saves.

    Per *thread*, not per instance, and that is not a detail. ``requests``
    hands each thread its own pooled connection, so callers were free to share
    one transport across threads — and the play route does exactly that: the
    media-segments prefetch runs beside the resolve, and the sidecar subtitle
    fetches run four at a time, all on the transport the route was handed.
    A single ``http.client`` connection cannot serve that: two threads writing
    requests onto one socket interleave, and the reply the other one reads is
    not its own. Seen live as ``[Errno 9] Bad file descriptor`` out of a
    PlaybackInfo POST.
    """

    def __init__(self, verify_ssl: bool = True) -> None:
        super().__init__(verify_ssl)
        self._local = threading.local()
        # Every connection this transport has opened, so close() can release
        # the ones belonging to threads that have since finished.
        self._all: List[http.client.HTTPConnection] = []
        self._all_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Release every connection this transport opened, on any thread.

        End-of-life only. A failure mid-exchange drops just the calling
        thread's connection (:meth:`_drop`) — closing another thread's socket
        from under it is the very race this class exists to avoid.
        """
        with self._all_lock:
            connections, self._all = list(self._all), []
        for connection in connections:
            try:
                connection.close()
            except Exception as error:  # pragma: no cover - defensive
                LOG.debug("connection close failed: %s", error)
        self._local = threading.local()

    def _drop(self) -> None:
        """Close and forget the calling thread's connection."""
        connection = getattr(self._local, "conn", None)
        if connection is None:
            return
        try:
            connection.close()
        except Exception as error:  # pragma: no cover - defensive
            LOG.debug("connection close failed: %s", error)
        with self._all_lock:
            if connection in self._all:
                self._all.remove(connection)
        self._local.conn = None
        self._local.origin = None

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if not self._verify_ssl:
            # The user turned verification off deliberately (a private CA, a
            # self-signed box). Same posture as the requests transport.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _connection(
        self, origin: Tuple[str, str, int], connect_timeout: float, read_timeout: float
    ) -> Tuple[http.client.HTTPConnection, bool]:
        """The pooled connection for ``origin``, and whether it is brand new.

        Freshness is what makes the stale-socket retry in :meth:`request` safe:
        a reused connection can be dead before the request is written, and then
        the server never saw it.
        """
        existing = getattr(self._local, "conn", None)
        if existing is not None and getattr(self._local, "origin", None) == origin:
            return existing, False

        self._drop()
        scheme, host, port = origin
        connection: http.client.HTTPConnection
        if scheme == "https":
            connection = http.client.HTTPSConnection(
                host, port, timeout=connect_timeout, context=self._context()
            )
        else:
            connection = http.client.HTTPConnection(host, port, timeout=connect_timeout)
        # Connect explicitly so the connect budget is the connect budget, and
        # the (longer) read budget applies only once a socket exists — the
        # split requests gives as a (connect, read) tuple.
        connection.connect()
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        self._local.conn = connection
        self._local.origin = origin
        with self._all_lock:
            self._all.append(connection)
        return connection, True

    # -- the request -----------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[Tuple[float, float]] = None,
        retries: Optional[int] = None,
    ) -> Any:
        if retries is None:
            retries = METHOD_RETRIES.get(method.upper(), 0)
        connect_timeout, read_timeout = timeout or DEFAULT_TIMEOUT
        origin, target = _split(url, params)
        body, send_headers = _body(json_body, headers)

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            if attempt:
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                time.sleep(delay + random.uniform(0, delay / 2))
            try:
                response = self._exchange(
                    origin,
                    method,
                    target,
                    body,
                    send_headers,
                    connect_timeout,
                    read_timeout,
                    url,
                )
            except _TRANSPORT_ERRORS as error:
                LOG.debug(
                    "attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    retries + 1,
                    url,
                    error,
                )
                last_error = error
                continue

            # Logged for every request, not just failures: the scenario gates
            # assert request counts, and those are ungreppable otherwise.
            LOG.debug("http %s %s -> %d", method, response.url, response.status_code)

            if response.status_code in (401, 403):
                raise Unauthorized("%s %s -> %d" % (method, url, response.status_code))
            if response.status_code >= 400:
                raise HttpError(
                    response.status_code,
                    "%s %s -> %d" % (method, url, response.status_code),
                )
            return response

        raise ServerUnreachable("%s %s: %s" % (method, url, last_error))

    def _exchange(
        self,
        origin: Tuple[str, str, int],
        method: str,
        target: str,
        body: Optional[bytes],
        headers: Dict[str, str],
        connect_timeout: float,
        read_timeout: float,
        url: str,
    ) -> Response:
        """One request/response, with one free retry on a dead pooled socket.

        A connection taken from the pool can have been closed by the server
        while it sat idle; the failure then happens before the request reaches
        anyone, so replaying it is safe for *any* method and must not spend the
        caller's retry budget (which for POST is zero, deliberately). A failure
        while reading the body is a different thing — the server did act — and
        propagates.
        """
        connection, fresh = self._connection(origin, connect_timeout, read_timeout)
        try:
            connection.request(method, target, body=body, headers=headers)
            raw = connection.getresponse()
        except _TRANSPORT_ERRORS:
            self._drop()
            if fresh:
                raise
            LOG.debug("pooled connection was dead; reconnecting for %s", url)
            connection, _ = self._connection(origin, connect_timeout, read_timeout)
            connection.request(method, target, body=body, headers=headers)
            raw = connection.getresponse()

        try:
            payload = raw.read()
        except _TRANSPORT_ERRORS:
            self._drop()
            raise
        if raw.will_close:
            # The server is done with this socket; do not offer it to the next
            # call as though it were alive.
            self._drop()
        return Response(raw.status, payload, url)


def _split(
    url: str, params: Optional[Dict[str, Any]]
) -> Tuple[Tuple[str, str, int], str]:
    """((scheme, host, port), request target) with ``params`` folded in."""
    parts = urlsplit(url)
    scheme = parts.scheme or "http"
    port = parts.port or (443 if scheme == "https" else 80)
    target = parts.path or "/"
    query = parts.query
    if params:
        # quote_via=quote to match what requests sends: a space becomes %20,
        # not '+'. Values are str()'d the same way, so booleans stay "True".
        extra = urlencode(
            {key: value for key, value in params.items() if value is not None},
            quote_via=quote,
        )
        query = "%s&%s" % (query, extra) if query else extra
    if query:
        target = "%s?%s" % (target, query)
    return (scheme, parts.hostname or "", port), target


def _body(
    json_body: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]
) -> Tuple[Optional[bytes], Dict[str, str]]:
    send_headers = dict(headers or {})
    body: Optional[bytes] = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        send_headers["Content-Type"] = "application/json"
        send_headers["Content-Length"] = str(len(body))
    # Identity, and stated rather than left to chance: http.client does not
    # decode content, so a server that decided to gzip would hand back bytes
    # nothing here would unpack. Jellyfin does not compress these responses
    # (measured: an Accept-Encoding request returned identical byte counts),
    # so nothing is being given up.
    send_headers.setdefault("Accept-Encoding", "identity")
    return body, send_headers
