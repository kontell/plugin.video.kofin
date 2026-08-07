"""The thread dump exists to answer one question about a rare event: was the
thread blocked, or just slow? So these tests park a real thread on a real lock
and check the dump can tell."""

import threading

from kofin.core import diag


def _capture(monkeypatch):
    lines = []
    monkeypatch.setattr("xbmc.log", lambda msg, level=0: lines.append(msg))
    return lines


def test_dump_names_a_blocked_thread_and_the_call_blocking_it(monkeypatch):
    lines = _capture(monkeypatch)
    held = threading.Lock()
    held.acquire()
    running = threading.Event()

    def wait_on_the_lock():
        running.set()
        held.acquire()

    blocked = threading.Thread(target=wait_on_the_lock, name="kofin-test-blocked")
    blocked.start()
    running.wait(5)

    try:
        positions = diag.thread_dump("under test")
    finally:
        held.release()
        blocked.join(5)

    dump = "\n".join(lines)
    assert "kofin-test-blocked" in dump
    # The frame that matters is the innermost one: the acquire it is parked on,
    # not the thread's entry point.
    assert "wait_on_the_lock" in dump
    assert "held.acquire()" in dump
    assert positions[blocked.ident].endswith("wait_on_the_lock")


def test_a_thread_parked_on_the_same_line_reads_as_stuck():
    before = {7: "library.py:540 service"}
    after = {7: "library.py:540 service"}
    assert "did not move" in diag.describe_movement(7, before, after)
    assert "library.py:540" in diag.describe_movement(7, before, after)


def test_a_thread_that_got_somewhere_reads_as_moving():
    moved = diag.describe_movement(
        7, {7: "library.py:540 service"}, {7: "movies.py:80 movie"}
    )
    assert moved.startswith("moved:")
    assert "movies.py:80" in moved


def test_movement_of_a_thread_missing_from_a_dump_is_not_claimed():
    """A thread that died between the two looks has no verdict to give, and
    inventing one ("moved") would read as progress that never happened."""
    assert "not in both" in diag.describe_movement(7, {7: "a.py:1 f"}, {})
    assert "no id" in diag.describe_movement(None, {}, {})


def test_deep_stacks_are_truncated_to_the_innermost_frames(monkeypatch):
    """A dump is only useful if it is readable: the writers nest deeply and
    the frames that explain a stall are the inner ones."""
    lines = _capture(monkeypatch)

    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        return diag.thread_dump("deep")

    recurse(diag.FRAMES + 20)

    dump = "\n".join(lines)
    assert "outer frame(s) omitted" in dump
    assert dump.count("in recurse") <= diag.FRAMES


def test_the_dump_is_one_log_entry(monkeypatch):
    """Kodi interleaves every thread's logging; a stack split over a hundred
    entries is unreadable by the time it reaches the log."""
    lines = _capture(monkeypatch)
    diag.thread_dump("single entry")
    assert len(lines) == 1
    assert "thread dump (single entry)" in lines[0]
