import os
import sys

import pytest

_LIB = os.environ.get("KOFIN_TEST_PACKAGE") or os.path.join(
    os.path.dirname(__file__), "..", "..", "lib"
)
sys.path.insert(0, os.path.abspath(_LIB))

# Several legacy tests import service modules during collection. Select their
# SQL contract before those conditional imports, on either source branch.
# The actual API package subprocesses deliberately bypass this override.
if not os.environ.get("KOFIN_TEST_PACKAGE"):
    from kofin import buildconfig

    buildconfig.BACKEND = "sql"


@pytest.fixture(autouse=True)
def play_queue_dir(tmp_path, monkeypatch):
    """Point the play queue at a per-test directory.

    The queue is a directory of claimable files rather than a window property
    (core/state.py), so every test that resolves or claims a playback needs a
    real place to put them — and needs it isolated, since claiming is a
    filesystem operation and entries left by one test would affect the next.
    """
    from kofin.core import state

    queue_dir = tmp_path / "playqueue"
    monkeypatch.setattr(state, "_queue_dir", lambda: str(queue_dir))


@pytest.fixture(autouse=True)
def unit_backend(monkeypatch):
    """Keep legacy contract tests on SQL on both branches. Package tests run
    the selected distribution in a separate process without this override."""
    if not os.environ.get("KOFIN_TEST_PACKAGE"):
        from kofin import buildconfig

        monkeypatch.setattr(buildconfig, "BACKEND", "sql")
