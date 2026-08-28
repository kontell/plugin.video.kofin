"""L1: the service seam helpers (service/ports.py)."""

import threading

from kofin.service.ports import forward, spawn_once


def test_spawn_once_starts_a_named_daemon_and_returns_it():
    ran = threading.Event()
    thread = spawn_once(None, ran.set, "kofin-test-oneshot")
    assert thread is not None
    thread.join(2)
    assert ran.is_set()
    assert thread.daemon is True
    assert thread.name == "kofin-test-oneshot"


def test_spawn_once_refuses_while_the_previous_run_lives():
    release = threading.Event()
    first = spawn_once(None, release.wait, "kofin-test-busy", 5)
    assert first is not None
    try:
        assert spawn_once(first, lambda: None, "kofin-test-busy") is None
    finally:
        release.set()
        first.join(2)

    again = spawn_once(first, lambda: None, "kofin-test-busy")
    assert again is not None
    again.join(2)


def test_forward_reaches_the_named_hook_and_swallows_its_failure():
    class Manager:
        def __init__(self):
            self.calls = []

        def on_wake(self, *args):
            self.calls.append(args)

        def on_sleep(self):
            raise RuntimeError("boom")

    manager = Manager()
    forward(manager, "on_wake", 1, 2)
    assert manager.calls == [(1, 2)]
    forward(manager, "on_sleep")  # swallowed, logged
    forward(None, "on_wake")  # no manager: a quiet no-op
