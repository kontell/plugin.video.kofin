"""The heap-release helper never fails, whatever the C library offers."""

from kofin.core import memory


def test_release_runs_and_reports(monkeypatch):
    assert memory.release() in (True, False)

    class NoLibc:
        def __init__(self, *_):
            raise OSError("no libc")

    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", NoLibc)
    assert memory.release() is False
