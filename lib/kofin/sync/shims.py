# -*- coding: utf-8 -*-
"""Fork-compat helpers for the transplanted sync code.

The sync transplant (see docs/phase2-implementation-plan.md §2) ports the
fork's writers and pipeline with mechanical adaptation only. This module is
the shim layer those ports import instead of the fork's ``helper`` package:
same names, same semantics, kofin plumbing underneath. Do not "improve"
behavior here — the writers were proven against these exact semantics.

Allowed module-level state: the shared xbmc.Monitor used by the ``stop``
wrapper. Creating a Monitor per wrapped call registers a new announce
receiver with Kodi each time, which adds up over a large sync. The instance
is stateless with respect to service restarts, so it is exempt from the
no-module-globals rule that protects the restart path.
"""

import datetime
import re
import threading
from functools import wraps

import xbmc
import xbmcgui

from kofin.core import settings, state, toast
from kofin.core.log import Logger

LOG = Logger(__name__)

ADDON_NAME = "Kofin"


class LibraryException(Exception):
    pass


class LibraryExitException(LibraryException):
    """Raised to propagate application/service exit through the sync stack."""


class LibraryOrphanException(LibraryException):
    """A child item whose parent could not be resolved or fetched.

    Kofin-only, and an exception rather than the fork's ``return False``
    because that value is already taken: ``movie``, ``musicvideo`` and
    ``episode`` all return it for the *unchanged* short-circuit, which is a
    success. A caller could not tell "nothing to do" from "this never
    landed", so the failure went unreported -- the UpdateWorker's handler
    only ever ran on exceptions, which is exactly the reporting this needs.
    """


_monitor_lock = threading.Lock()
_monitor = None


def _get_monitor():
    global _monitor

    with _monitor_lock:
        if _monitor is None:
            _monitor = xbmc.Monitor()

    return _monitor


def raise_if_stopping():
    """Raise LibraryExitException when Kodi is exiting, the service is
    shutting down, or the server went offline -- the three conditions the
    ``stop`` decorator checks, callable from inside a loop (the pager asks
    before every page it yields; a decorator on a generator only ever asked
    once, at creation)."""
    if _get_monitor().abortRequested():
        raise LibraryExitException("Kodi aborted, exiting...")

    if state.should_stop():
        raise LibraryExitException("Should stop flag raised, exiting...")

    if not state.is_online():
        raise LibraryExitException("Server not online, exiting...")


def stop(func):
    """Abort the wrapped call when Kodi exits, the service shuts down, or the
    server went offline (fork ``helper.wrapper.stop`` semantics).
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        raise_if_stopping()
        return func(*args, **kwargs)

    return wrapper


def jellyfin_item(func):
    """Wrapper to retrieve the kofin.db reference row for the item."""

    @wraps(func)
    def wrapper(self, item, *args, **kwargs):
        e_item = self.jellyfin_db.get_item_by_id(
            item["Id"] if isinstance(item, dict) else item
        )

        return func(self, item, *args, e_item=e_item, **kwargs)

    return wrapper


def progress(message=None):
    """Start and close a background progress dialog around the call.

    ``message`` may be a string id (int) — localized when the dialog opens,
    not when the decorator is applied at import time.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self, item=None, *args, **kwargs):

            dialog = xbmcgui.DialogProgressBG()
            text = localized(message) if isinstance(message, int) else message

            if item and isinstance(item, dict):
                dialog.create(ADDON_NAME, "%s %s" % (localized(30400), item["Name"]))
                LOG.info("Processing %s: %s", item["Name"], item["Id"])
            else:
                dialog.create(ADDON_NAME, text)
                LOG.info("Processing %s", text)

            if item:
                args = (item,) + args

            try:
                return func(self, *args, dialog=dialog, **kwargs)
            finally:
                dialog.close()

        return wrapper

    return decorator


def notification(message, time_ms=5000, sound=False, error=False, warning=False):
    """Non-modal toast. The sync stack never raises modal dialogs from
    service threads (report reliability policy).

    ``error``/``warning`` keep the fork's keyword shape; the icon each one
    means is core/toast.py's to decide. ``warning`` is a kofin addition, for
    the sync messages that refuse or advise rather than report a failure.
    """
    level = toast.ERROR if error else toast.WARNING if warning else toast.INFO
    toast.show(message, level, heading=ADDON_NAME, time_ms=time_ms, sound=sound)


def localized(string_id):
    return settings.localized(string_id)


def values(item, keys):
    """Grab the values in the item for a list of keys {key},{key1}....
    If the key has no brackets, the key will be passed as is.
    """
    return (
        (
            item[key.replace("{", "").replace("}", "")]
            if isinstance(key, str) and key.startswith("{")
            else key
        )
        for key in keys
    )


def split_list(itemlist, size):
    """Split up list in pieces of size. Will generate a list of lists"""
    return [itemlist[i : i + size] for i in range(0, len(itemlist), size)]


_DATE_RE = re.compile(
    r"^(\d{1,4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
)


def convert_to_local(date):
    """Convert a Jellyfin UTC timestamp to local time, fork output shape
    (``YYYY-MM-DDTHH:MM:SS``). Handles 7-digit tick fractions and the
    year-1 "no date" sentinel without dateutil.
    """
    try:
        if isinstance(date, datetime.datetime):
            parsed = date
        else:
            match = _DATE_RE.match(str(date))
            if not match:
                raise ValueError("unrecognized date: %r" % date)
            year, month, day, hour, minute, second = (
                int(part) for part in match.groups()[:6]
            )
            parsed = datetime.datetime(year, month, day, hour, minute, second)

        parsed = parsed.replace(tzinfo=datetime.timezone.utc)

        # Bad metadata defaults to date 1-1-1. astimezone() on it can
        # underflow, and it only means "no date" anyway.
        if parsed.year < 1900:
            return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
                parsed.year,
                parsed.month,
                parsed.day,
                parsed.hour,
                parsed.minute,
                parsed.second,
            )

        return parsed.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    except Exception as error:
        LOG.exception("Item date: %s --- %s", str(date), error)

        return str(date)


Local = convert_to_local


def window_prop(key, value=None, clear=False):
    """Plain string window-property access on the home window (the fork's
    ``window()`` helper minus the .json/.bool suffixes, which the ported
    views code does not use)."""
    window = xbmcgui.Window(10000)

    if clear:
        window.clearProperty(key)
        return None
    if value is not None:
        window.setProperty(key, value)
        return None
    return window.getProperty(key)
