"""Ownership and restart contracts of the private store."""

import pytest

from kofin.sync import private
from kofin.sync.catalogue import Catalogue, BackendMismatch, claim_backend
from kofin.sync.model import MediaItem, Metadata


def media(title="Movie"):
    return MediaItem.from_dto({"Id": "m1", "Type": "Movie", "Name": title})


@pytest.fixture
def store(tmp_path):
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield Catalogue("server-1/user-1")
    private.reset_overrides()


def test_desired_is_durable_and_only_current_readback_advances_applied(store):
    first = store.stage(media())
    assert first == store.stage(media())
    assert store.state("m1").applied == 0
    reopened = Catalogue("server-1/user-1")
    assert reopened.pending() == [(media(), first, "upsert")]
    reopened.failed("m1", first, "timeout")
    second = reopened.stage(media("Changed"))
    assert second > first
    assert not reopened.confirm("m1", first)
    assert reopened.state("m1").applied == 0
    assert reopened.confirm("m1", second)
    assert reopened.state("m1").applied == second
    assert reopened.pending() == []
    removed = reopened.stage(media("Changed"), "remove")
    assert reopened.state("m1").applied == second
    assert reopened.pending()[0][1:] == (removed, "remove")
    assert Catalogue("server-2/user-1").pending() == []


def test_sql_and_api_mappings_are_never_adopted_across_backends(store):
    with private.Database() as db:
        claim_backend(db.cursor, "sql")
        db.cursor.execute(
            "INSERT INTO view VALUES ('shared', 'Still readable', 'movies')"
        )
    with pytest.raises(BackendMismatch):
        store.stage(media())
    with private.Database() as db:
        assert (
            db.cursor.execute("SELECT view_name FROM view").fetchone()[0]
            == "Still readable"
        )


def test_unmarked_legacy_mappings_require_fresh_state(store):
    with private.Database() as db:
        db.cursor.execute(
            "INSERT INTO jellyfin(jellyfin_id, kodi_id) VALUES ('old', 42)"
        )
    with pytest.raises(BackendMismatch):
        store.stage(media())


def test_metadata_reads_the_runtime_in_seconds():
    assert Metadata({"RunTimeTicks": 15_000_000}).get_runtime() == 1.5
