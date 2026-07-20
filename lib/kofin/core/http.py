"""HTTP transport: one persistent session per process, retries, error taxonomy.

Pure python (no Kodi imports) so the whole network stack is unit-testable.
"""

import random
import time
from typing import Any, Dict, Optional, Tuple

import requests

from kofin.core.log import Logger

LOG = Logger(__name__)

DEFAULT_TIMEOUT = (6.0, 30.0)
RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5


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


class Http:
    """A lazily created, kept-alive requests session."""

    def __init__(self, verify_ssl: bool = True) -> None:
        self._verify_ssl = verify_ssl
        self._session: Optional[requests.Session] = None

    def session(self) -> requests.Session:
        if self._session is None:
            session = requests.Session()
            session.verify = self._verify_ssl
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
        retries: int = RETRIES,
    ) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            if attempt:
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                time.sleep(delay + random.uniform(0, delay / 2))
            try:
                response = self.session().request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=timeout or DEFAULT_TIMEOUT,
                )
            except (requests.ConnectionError, requests.Timeout) as error:
                LOG.debug(
                    "attempt %d/%d failed for %s: %s",
                    attempt + 1,
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
                getattr(sent, "url", None) or url,
                response.status_code,
            )

            if response.status_code in (401, 403):
                raise Unauthorized("%s %s -> %d" % (method, url, response.status_code))
            if response.status_code >= 400:
                raise HttpError(
                    response.status_code,
                    "%s %s -> %d" % (method, url, response.status_code),
                )
            return response

        raise ServerUnreachable("%s %s: %s" % (method, url, last_error))
