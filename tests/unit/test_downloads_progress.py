"""L1 units for the aggregate download progress bar (plan W3.4)."""

import pytest

from kofin.downloads import progress, store
from kofin.sync import db as sync_db
from tests.unit.fakes import FakeAddon


class RecordingDialog:
    instances = []

    def __init__(self):
        self.created = None
        self.updates = []
        self.closed = False
        RecordingDialog.instances.append(self)

    def create(self, heading, message=""):
        self.created = (heading, message)

    def update(self, percent=0, heading="", message=""):
        self.updates.append((percent, message))

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    RecordingDialog.instances = []
    monkeypatch.setattr(progress.xbmcgui, "DialogProgressBG", RecordingDialog)
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    clock = {"now": 1000.0}
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock["now"])
    yield clock
    sync_db.reset_overrides()


def reporter(stopping=False):
    return progress.Reporter(lambda: stopping)


def seed(count):
    for index in range(count):
        store.queue(store.Download(jellyfin_id="i%d" % index, queued_at=100 + index))


def bar():
    return RecordingDialog.instances[0]


def test_the_bar_tracks_items_and_bytes(env):
    seed(2)
    store.claim()
    reporting = reporter()
    reporting.begin("i0", "First", 100)
    assert bar().created is not None

    env["now"] += 2
    reporting.tick("i0", 50)
    percent, message = bar().updates[-1]
    assert percent == 25  # (0 done + 0.5 of the active item) of 2
    assert message == "1/2 — First"

    store.finish("i0", "a/b.mkv", "mkv", 100)
    reporting.finish("i0", completed=True)
    assert not bar().closed  # one still pending: the frame stays

    store.claim()
    reporting.begin("i1", "Second", 100)
    percent, message = bar().updates[-1]
    assert percent == 50  # the completed item advanced the count
    assert message == "2/2 — Second"

    store.finish("i1", "a/c.mkv", "mkv", 100)
    reporting.finish("i1", completed=True)
    assert bar().closed  # the queue drained


def test_ticks_repaint_at_most_once_a_second(env):
    seed(1)
    store.claim()
    reporting = reporter()
    reporting.begin("i0", "X", 100)
    painted = len(bar().updates)

    reporting.tick("i0", 10)
    reporting.tick("i0", 20)
    assert len(bar().updates) == painted  # same second: suppressed

    env["now"] += 1.5
    reporting.tick("i0", 30)
    assert len(bar().updates) == painted + 1


def test_unknown_totals_count_only_on_completion(env):
    seed(2)
    store.claim()
    reporting = reporter()
    reporting.begin("i0", "T", 0)  # a transcode nobody could estimate
    env["now"] += 2
    reporting.tick("i0", 123456789)
    percent, _message = bar().updates[-1]
    assert percent == 0  # no invented percentage

    store.finish("i0", "a/t.mp4", "mp4", 1)
    reporting.finish("i0", completed=True)
    store.claim()
    reporting.begin("i1", "U", 0)
    percent, _message = bar().updates[-1]
    assert percent == 50  # it counted once it finished, and only then


def test_the_bar_steps_back_when_the_queue_grows(env):
    seed(1)
    store.claim()
    reporting = reporter()
    reporting.begin("i0", "X", 100)
    env["now"] += 2
    reporting.tick("i0", 100)
    assert bar().updates[-1][0] == 100

    store.queue(store.Download(jellyfin_id="late", queued_at=999))
    env["now"] += 2
    reporting.tick("i0", 100)
    assert bar().updates[-1][0] == 50  # honest, as the scanner's bar is


def test_nothing_appears_while_stopping(env):
    seed(1)
    store.claim()
    reporting = reporter(stopping=True)
    reporting.begin("i0", "X", 100)
    assert RecordingDialog.instances == []
    reporting.close()  # safe when never created


def test_idle_closes_only_when_nothing_moves(env):
    """The offline-hold shape: the interrupted item is released back to
    queued, so the remainder is non-zero — finish keeps the frame (a busy
    queue's between-items gap must not flicker) and the worker's hold
    branch is what closes it."""
    seed(2)
    store.claim()
    reporting = reporter()
    reporting.begin("i0", "X", 100)

    reporting.idle()  # another worker with nothing to do
    assert not bar().closed  # a transfer is live

    store.release("i0")
    reporting.finish("i0", completed=False)  # the retry path: no advance
    assert not bar().closed  # remainder > 0 keeps the frame

    reporting.idle()  # the hold branch, nothing moving now
    assert bar().closed


def test_close_is_idempotent(env):
    reporting = reporter()
    reporting.close()
    seed(1)
    store.claim()
    reporting.begin("i0", "X", 100)
    reporting.close()
    reporting.close()
    assert bar().closed
