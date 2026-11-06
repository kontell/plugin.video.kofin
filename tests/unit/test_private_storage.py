"""Private state opens kofin.db and nothing else."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from kofin.sync import private


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


def test_the_schema_drops_the_tables_nothing_reads():
    """A profile an earlier build filed holds playlist_item, boxset_state and
    version; the schema drops them and keeps the tables it reads."""
    import sqlite3

    cursor = sqlite3.connect(":memory:").cursor()
    cursor.execute("CREATE TABLE playlist_item(playlist_id TEXT, jellyfin_id TEXT)")
    cursor.execute("CREATE INDEX playlist_item_song ON playlist_item(jellyfin_id)")
    cursor.execute("CREATE TABLE boxset_state(jellyfin_id TEXT)")
    cursor.execute("CREATE TABLE version(idVersion TEXT)")

    private.kofin_tables(cursor)

    names = {
        row[0] for row in cursor.execute("SELECT name FROM sqlite_master").fetchall()
    }
    assert not names & {
        "playlist_item",
        "playlist_item_song",
        "boxset_state",
        "version",
    }
    assert {"view", "playlist_state", "download"} <= names
    assert "jellyfin" not in names


def test_a_download_table_without_the_validator_gains_it():
    """A profile an earlier build filed has the download table without the
    column a resume's If-Range is read from."""
    import sqlite3

    cursor = sqlite3.connect(":memory:").cursor()
    cursor.execute("""CREATE TABLE download(
        jellyfin_id TEXT PRIMARY KEY, media_type TEXT, series_id TEXT,
        state TEXT, origin TEXT, rel_path TEXT, container TEXT,
        size_expected INTEGER, size_actual INTEGER, quality TEXT,
        bytes_done INTEGER, userdata_json TEXT,
        queued_at INTEGER, done_at INTEGER, error TEXT,
        segments_json TEXT,
        request_id TEXT, request_name TEXT)""")

    private.kofin_tables(cursor)

    columns = {row[1] for row in cursor.execute("PRAGMA table_info(download)")}
    assert "validator" in columns
