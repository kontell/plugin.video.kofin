"""Private state must work with all native database modules unavailable."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from kofin.sync import db, private


def test_private_import_and_roundtrip_without_native_modules(tmp_path):
    code = r"""
import importlib.abc
import sqlite3
import sys
from pathlib import Path

class RejectNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('kofin.sync.db', 'kofin.sync.schema') or fullname.startswith('kofin.sync.kodidb'):
            raise AssertionError('native import: ' + fullname)
sys.meta_path.insert(0, RejectNative())
root = Path(sys.argv[1])
def guard(event, args):
    if event == 'sqlite3.connect':
        assert Path(args[0]).resolve() == root / 'kofin.db'
sys.addaudithook(guard)
from kofin.sync.private import Database, set_path_override
set_path_override('kofin', str(root / 'kofin.db'))
with Database() as opened:
    opened.cursor.execute("INSERT INTO view VALUES ('v1','Movies','movies')")
with Database() as opened:
    assert opened.cursor.execute('SELECT view_id FROM view').fetchone() == ('v1',)
"""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root / "lib"))
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["video", "music", "texture", "/tmp/MyVideos149.db"])
def test_private_connection_refuses_native_kinds_and_arbitrary_paths(name):
    with pytest.raises(ValueError, match="Kofin-owned"):
        with private.Database(name):
            pass


def test_legacy_and_private_callers_share_one_override_and_schema(tmp_path):
    db.reset_overrides()
    try:
        db.set_path_override("kofin", str(tmp_path / "kofin.db"))
        with db.Database("kofin") as legacy:
            legacy.cursor.execute("INSERT INTO view VALUES ('v1','Movies','movies')")
        with private.Database() as opened:
            assert opened.cursor.execute("SELECT view_id FROM view").fetchone() == (
                "v1",
            )
        with pytest.raises(RuntimeError):
            with private.Database() as opened:
                opened.cursor.execute("DELETE FROM view")
                raise RuntimeError("rollback")
        with db.Database("kofin") as legacy:
            assert legacy.cursor.execute("SELECT COUNT(*) FROM view").fetchone()[0] == 1
    finally:
        db.reset_overrides()
