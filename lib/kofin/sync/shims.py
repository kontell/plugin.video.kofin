# -*- coding: utf-8 -*-
"""Small Kodi helpers the sync code shares.

What is left of the fork-compat layer the transplanted SQL writers imported:
a localized-string lookup, the home-window property access the node
publisher uses, and the UTC-to-local conversion the API serializer stamps
``dateadded`` and ``lastplayed`` with (``metadata._timestamp``).
"""

import datetime
import re

import xbmcgui

from kofin.core import settings
from kofin.core.log import Logger

LOG = Logger(__name__)


def localized(string_id):
    return settings.localized(string_id)


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
