import os
import sys

import pytest

_LIB = os.environ.get("KOFIN_TEST_PACKAGE") or os.path.join(
    os.path.dirname(__file__), "..", "..", "lib"
)
sys.path.insert(0, os.path.abspath(_LIB))


@pytest.fixture(autouse=True)
def play_queue_dir(tmp_path, monkeypatch):
    """Point the play queue at a per-test directory.

    The queue is a directory of claimable files, so entries one test leaves
    behind would be claimed by the next.
    """
    from kofin.core import state

    queue_dir = tmp_path / "playqueue"
    monkeypatch.setattr(state, "_queue_dir", lambda: str(queue_dir))
