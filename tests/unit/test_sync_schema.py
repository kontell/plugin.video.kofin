import os

import pytest

from kofin.sync import schema
from tests.unit import kodifixtures


@pytest.fixture(autouse=True)
def clean_cache():
    schema.reset_cache()
    yield
    schema.reset_cache()


def fake_database_dir(monkeypatch, files):
    monkeypatch.setattr("xbmcvfs.listdir", lambda path: ([], list(files)))
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: "/kodi/database")


def test_discover_picks_newest_matching_file(monkeypatch):
    fake_database_dir(
        monkeypatch,
        ["MyVideos121.db", "MyVideos131.db", "MyVideos131.db-wal", "Textures13.db"],
    )
    assert schema.discover("video") == ("MyVideos131.db", 131)


def test_discover_ignores_journal_suffixes(monkeypatch):
    fake_database_dir(
        monkeypatch,
        ["MyMusic83.db-shm", "MyMusic83.db-journal", "MyMusic82.db", "MyMusic83.db"],
    )
    assert schema.discover("music") == ("MyMusic83.db", 83)


def test_missing_database_raises(monkeypatch):
    fake_database_dir(monkeypatch, ["Textures13.db"])
    with pytest.raises(schema.DatabaseMissing):
        schema.check("video")


def test_supported_omega_versions_pass_the_gate(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos131.db", "MyMusic83.db"])
    assert schema.check("video") == 131
    assert schema.check("music") == 83
    assert schema.gate_status() is None


def test_supported_piers_versions_pass_the_gate(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos146.db", "MyMusic84.db"])
    assert schema.check("video") == 146
    assert schema.check("music") == 84
    assert schema.gate_status() is None


def test_supported_piers_147_versions_pass_the_gate(monkeypatch):
    """Piers bumped MyVideos to 147 mid-beta. An install that never ran the
    newer build still has 146, so both numbers are in the wild and both pass;
    music did not move with it."""
    fake_database_dir(monkeypatch, ["MyVideos147.db", "MyMusic84.db"])
    assert schema.check("video") == 147
    assert schema.check("music") == 84
    assert schema.gate_status() is None


def test_discovery_prefers_147_over_a_left_behind_146(monkeypatch):
    """Kodi leaves the old file behind when it migrates, so both sit in the
    directory and the newest is the live one."""
    fake_database_dir(monkeypatch, ["MyVideos146.db", "MyVideos147.db"])
    assert schema.discover("video") == ("MyVideos147.db", 147)


def test_unknown_version_is_refused(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos999.db", "MyMusic83.db"])
    with pytest.raises(schema.SchemaUnsupported) as excinfo:
        schema.check("video")
    assert excinfo.value.version == 999
    assert excinfo.value.kind == "video"

    failure = schema.gate_status()
    assert isinstance(failure, schema.SchemaUnsupported)


def test_gate_status_scopes_to_requested_kinds(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos131.db", "MyMusic999.db"])
    assert schema.gate_status(("video",)) is None
    failure = schema.gate_status(("video", "music"))
    assert isinstance(failure, schema.SchemaUnsupported)
    assert failure.version == 999


def test_database_path_joins_dir_and_gate(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos131.db"])
    assert schema.database_path("video") == "/kodi/database/MyVideos131.db"


def test_texture_is_version_gated_like_the_others(monkeypatch):
    # Gated since the chapter-thumb feature writes it: known versions pass,
    # an unknown one refuses (seeding then disables itself, playback lives).
    fake_database_dir(monkeypatch, ["Textures13.db"])
    assert schema.check("texture") == 13
    schema.reset_cache()
    fake_database_dir(monkeypatch, ["Textures99.db"])
    with pytest.raises(schema.SchemaUnsupported):
        schema.check("texture")


def test_every_supported_video_version_is_backed(monkeypatch):
    """The gate may only name versions the suite can actually prove: a fixture
    to write against, and the asset-type number the extras pass needs. Opening
    the gate without either is how a schema bump becomes silent corruption."""
    for version in schema.SUPPORTED["video"]:
        assert os.path.exists(
            os.path.join(kodifixtures.FIXTURES, "myvideos%d.sql" % version)
        ), ("no schema fixture for MyVideos%d" % version)
        assert os.path.exists(
            os.path.join(kodifixtures.FIXTURES, "myvideos%d_seed.sql" % version)
        ), ("no seed fixture for MyVideos%d" % version)
        assert version in schema.EXTRA_ITEM_TYPE


def test_every_supported_music_and_texture_version_is_backed():
    for version in schema.SUPPORTED["music"]:
        assert os.path.exists(
            os.path.join(kodifixtures.FIXTURES, "mymusic%d.sql" % version)
        )
        assert os.path.exists(
            os.path.join(kodifixtures.FIXTURES, "mymusic%d_seed.sql" % version)
        )
        # The cleaner re-inserts these after its wipe; a music version
        # without stated seeds would wipe below pristine (plan G2).
        assert version in schema.MUSIC_SEED_SQL
    for version in schema.SUPPORTED["texture"]:
        assert os.path.exists(
            os.path.join(kodifixtures.FIXTURES, "textures%d.sql" % version)
        )
        assert version in schema.CHAPTER_ART_WRAPPED
