"""What the shipped package carries, checked on the extracted bytes."""

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

from tests.unit.test_build import _build_module


def _staged(tmp_path):
    build = _build_module()
    stage = tmp_path / "addon"
    build.stage(stage)
    return stage


def test_zip_and_development_install_have_identical_contents(tmp_path):
    build = _build_module()
    archive = build.build(tmp_path / "zip")
    stage = tmp_path / "stage"
    build.stage(stage)
    with zipfile.ZipFile(archive) as zipped:
        contents = {
            name.split("/", 1)[1]: zipped.read(name) for name in zipped.namelist()
        }
    staged = {
        p.relative_to(stage).as_posix(): p.read_bytes()
        for p in stage.rglob("*")
        if p.is_file()
    }
    assert contents == staged
    assert "lib/kofin/sync/backends/api/requirements.json" in contents
    # Nothing of the SQL build ships: the tree has no native database code.
    assert not any(name.startswith("lib/kofin/sync/kodidb/") for name in contents)
    assert not any(name.startswith("lib/kofin/sync/writers/") for name in contents)
    assert "lib/kofin/buildconfig.py" not in contents
    settings_ids = {
        setting.get("id")
        for setting in ET.fromstring(contents["resources/settings.xml"]).iter("setting")
    }
    assert {"libraryNodes", "libraryStatus", "fullUpdateInterval"} <= settings_ids
    assert not {"syncMusicPlaylists", "syncStatus", "syncedLibraries"} & settings_ids
    entries = {"context_playlist.py", "context_download_playlist.py"}
    assert entries <= set(contents)
    assert entries <= {
        item.get("library")
        for item in ET.fromstring(contents["addon.xml"]).iter("item")
    }


def _settings(stage):
    tree = ET.parse(stage / "resources" / "settings.xml").getroot()
    return {setting.get("id"): setting for setting in tree.iter("setting")}


def _visible_while(setting):
    return {
        (condition.get("setting"), condition.text)
        for dependency in setting.findall("dependencies/dependency")
        if dependency.get("type") == "visible"
        for condition in dependency.iter("condition")
    }


def test_the_direct_play_lists_hide_under_force_direct_play(tmp_path):
    """A play under force direct play reads none of the three lists; hidden,
    they keep their values for a context-menu transcode and the download
    decision."""
    settings = _settings(_staged(tmp_path))
    for name in ("directPlayVideoCodecs", "directPlayAudioCodecs", "allowedHdrTypes"):
        assert ("forceDirectPlay", "false") in _visible_while(settings[name])
        # The rule comes before the control, where the settings schema has it.
        children = [child.tag for child in settings[name]]
        assert children.index("dependencies") < children.index("control")
    for name in ("forceRemux", "forceTranscode"):
        assert ("forceDirectPlay", "false") in _visible_while(settings[name])


def test_transcode_music_help_says_what_the_switch_does(tmp_path):
    """Every song is a plugin path, so the switch rewrites nothing and its
    help must not ask for a repair."""
    assert _settings(_staged(tmp_path))["musicTranscode"].get("help") == "30861"


def test_package_starts_with_kodi_database_access_forbidden(tmp_path):
    stage = _staged(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "tests/package/probe.py"),
            str(stage),
            str(profile),
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_package_preserves_live_browser_contracts(tmp_path):
    """Run the established listing/field/URL goldens against shipped code."""
    stage = _staged(tmp_path)
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_browse.py",
            "tests/unit/test_api_store.py",
            "tests/unit/test_api_metadata.py",
            "tests/unit/test_api_lifecycle.py",
            "tests/unit/test_api_coordinator.py",
            "tests/unit/test_api_provider.py",
            "tests/unit/test_api_music.py",
            "tests/unit/test_api_progress.py",
            "tests/unit/test_browse_golden.py",
            "tests/unit/test_listitems.py",
            "tests/unit/test_playall.py",
            "tests/unit/test_plugin_streams.py",
            "tests/unit/test_play.py::test_resume_true_starts_at_the_server_position",
            "tests/unit/test_play.py::test_play_from_beginning_builds_the_item_without_a_resume_point",
            "tests/unit/test_play.py::test_play_mediasourceid_selects_that_source",
            "tests/unit/test_play.py::test_stream_indices_travel_with_the_source_id",
            "tests/unit/test_context.py::test_manage_offers_the_watched_toggle_on_a_dynamic_item",
            "tests/unit/test_context.py::test_manage_options_reset_follows_the_watched_toggle_on_an_in_progress_row",
            "tests/unit/test_context.py::test_extras_is_offered_only_when_the_item_has_some",
            "tests/unit/test_plugin_actions.py",
            "-q",
            "--tb=short",
        ],
        cwd=root,
        env=dict(os.environ, KOFIN_TEST_PACKAGE=str(stage / "lib")),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_manifest_keeps_every_native_content_type_under_the_native_tree(tmp_path):
    addon = ET.parse(_staged(tmp_path) / "addon.xml").getroot()
    plugin = next(
        ext
        for ext in addon.findall("extension")
        if ext.get("point") == "xbmc.python.pluginsource"
    )
    assert {
        (path.get("content"), path.text)
        for path in plugin.findall("medialibraryscanpath")
    } == {("movies", "/native/"), ("tvshows", "/native/"), ("musicvideos", "/native/")}
