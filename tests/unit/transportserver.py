"""A scripted loopback HTTP server, for holding both transports to one contract.

``test_http.py`` and ``test_stdhttp.py`` fake the library underneath each
transport; that is right for the retry ladder and the timeout split, and
blind to everything the real wire does — a redirect the library follows on
its own, a body that is not JSON, a status the ladder never sees. The
transport contract test (audit fixes plan H0) runs the two real transports
against this server and asserts the *same* answer from both.

Usage::

    with ScriptedServer() as server:
        server.answer("/Items", 200, json_body={"Items": []})
        server.answer("/Old", 302, headers={"Location": server.url + "/Items"})
        ...
        server.requests  # [(method, path, headers, body), ...] in wire order

Each ``answer`` is consumed by one request to that path (query string
ignored); ``repeat=True`` keeps it for every request. A path with no answer
left gets a 599 with an explanatory body, so a test that under-scripts fails
loudly rather than hanging.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

Answer = Tuple[int, Dict[str, str], bytes, bool]


class ScriptedServer:
    def __init__(self) -> None:
        self._script: Dict[str, List[Answer]] = {}
        self._lock = threading.Lock()
        self.requests: List[Tuple[str, str, Dict[str, str], bytes]] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- scripting -------------------------------------------------------------

    def answer(
        self,
        path: str,
        status: int,
        body: bytes = b"",
        headers: Optional[Dict[str, str]] = None,
        json_body: Any = None,
        repeat: bool = False,
    ) -> None:
        sent_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            sent_headers.setdefault("Content-Type", "application/json")
        with self._lock:
            self._script.setdefault(path, []).append(
                (status, sent_headers, body, repeat)
            )

    def _next(self, path: str) -> Answer:
        with self._lock:
            queue = self._script.get(path) or []
            if not queue:
                return (
                    599,
                    {"Content-Type": "text/plain"},
                    ("no scripted answer for %s" % path).encode("utf-8"),
                    False,
                )
            status, headers, body, repeat = queue[0]
            if not repeat:
                queue.pop(0)
            return status, headers, body, repeat

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> "ScriptedServer":
        script = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                with script._lock:
                    script.requests.append(
                        (self.command, path, dict(self.headers.items()), body)
                    )
                status, headers, payload, _ = script._next(path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = do_HEAD = do_PUT = _serve

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass  # keep pytest output clean

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="scripted-http", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def url(self) -> str:
        assert self._server is not None, "server not started"
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def __enter__(self) -> "ScriptedServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
