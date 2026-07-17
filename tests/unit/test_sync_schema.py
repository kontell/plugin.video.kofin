import pytest

from kofin.sync import schema


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


def test_supported_versions_pass_the_gate(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos131.db", "MyMusic83.db"])
    assert schema.check("video") == 131
    assert schema.check("music") == 83
    assert schema.gate_status() is None


def test_unknown_version_is_refused(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos146.db", "MyMusic83.db"])
    with pytest.raises(schema.SchemaUnsupported) as excinfo:
        schema.check("video")
    assert excinfo.value.version == 146
    assert excinfo.value.kind == "video"

    failure = schema.gate_status()
    assert isinstance(failure, schema.SchemaUnsupported)


def test_gate_status_scopes_to_requested_kinds(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos131.db", "MyMusic84.db"])
    assert schema.gate_status(("video",)) is None
    failure = schema.gate_status(("video", "music"))
    assert isinstance(failure, schema.SchemaUnsupported)
    assert failure.version == 84


def test_database_path_joins_dir_and_gate(monkeypatch):
    fake_database_dir(monkeypatch, ["MyVideos131.db"])
    assert schema.database_path("video") == "/kodi/database/MyVideos131.db"


def test_texture_is_not_version_gated(monkeypatch):
    fake_database_dir(monkeypatch, ["Textures99.db"])
    assert schema.check("texture") == 99
