"""Scaffolding for driving ``Library.service()`` -- the sync tick -- one
call at a time (P2.0c, the harness P2.3 refactors against).

Everything the tick touches that would otherwise reach Kodi, the network or
a thread is replaced here: the worker classes record construction instead
of running, ``FakeWorker`` stands in for a live or finished thread, and
``Clock`` owns both clocks the tick reads (``time.time`` for the download
backoff and ``datetime.now`` for the retry ladder).
"""

import datetime as _datetime
import time as _time
from typing import Any, Dict, List

from kofin.sync import library as library_module
from kofin.sync.library import Library
from tests.unit.test_sync_library import FakeApi, FakePlayer, _FakeMonitor


class FakeSession:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeWorker:
    """A thread the reaper sees: done or not, tagged like the real ones."""

    def __init__(self, is_done=False, source=None, db_file=None, unreachable=False):
        self.is_done = is_done
        self.source = source
        self.db_file = db_file
        self.unreachable = unreachable
        self.server = FakeSession()


class RecordingWorker:
    """Stands in for a worker class: records the construction, never runs."""

    instances: List["RecordingWorker"] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.is_done = False
        self.started = False
        self.server = FakeSession()
        RecordingWorker.instances.append(self)

    def start(self):
        self.started = True


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def datetime_now(self):
        return _datetime.datetime.fromtimestamp(self.now)

    def advance(self, seconds):
        self.now += seconds


def make_ticking_library(monkeypatch, clock=None):
    """A Library whose ``service()`` can be called directly.

    Returns ``(library, api, clock, spawned)`` where ``spawned`` is the
    list every stand-in worker class appends to on construction.
    """
    clock = clock or Clock()
    api = FakeApi()
    manager = Library(api, FakePlayer(), lambda: api)
    manager.monitor = _FakeMonitor()

    RecordingWorker.instances = []
    for name in (
        "GetItemWorker",
        "UpdateWorker",
        "UserDataWorker",
        "RemovedWorker",
        "SortWorker",
    ):
        monkeypatch.setattr(library_module, name, RecordingWorker)

    monkeypatch.setattr(library_module.time, "time", clock.time)
    # Kodistubs answers True to every condition, which would open the
    # playback gate through its live-TV clause.
    monkeypatch.setattr(library_module.xbmc, "getCondVisibility", lambda _c: False)

    class TickDatetime(_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.datetime_now()

    monkeypatch.setattr(library_module, "datetime", TickDatetime)

    return manager, api, clock, RecordingWorker.instances


def spawned_by(spawned, **attrs) -> List[Any]:
    """The recorded workers whose attributes match ``attrs``."""
    return [
        worker
        for worker in spawned
        if all(getattr(worker, key, None) == value for key, value in attrs.items())
    ]
