import os
import sys

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "lib")
sys.path.insert(0, os.path.abspath(_LIB))


@pytest.fixture(autouse=True)
def play_queue_dir(tmp_path, monkeypatch):
    """Point the play queue at a per-test directory.

    The queue is a directory of claimable files rather than a window property
    (core/state.py), so every test that resolves or claims a playback needs a
    real place to put them — and needs it isolated, since claiming is a
    filesystem operation and entries left by one test would be adopted by the
    next through the oldest-entry fallback.
    """
    from kofin.core import state

    queue_dir = tmp_path / "playqueue"
    monkeypatch.setattr(state, "_queue_dir", lambda: str(queue_dir))
