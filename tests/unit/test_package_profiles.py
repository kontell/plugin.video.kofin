"""The distribution boundary is checked on extracted package bytes."""

import os
from pathlib import Path
import subprocess
import sys
import zipfile

from tests.unit.test_build import _build_module


def test_zip_and_development_install_have_identical_profile_contents(tmp_path):
    build = _build_module()
    for profile in ("sql", "api"):
        archive = build.build(tmp_path / profile, profile)
        stage = tmp_path / (profile + "-stage")
        build.stage(stage, profile)
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
        assert ("lib/kofin/sync/db.py" in contents) == (profile == "sql")
        assert ("lib/kofin/sync/backends/sql/backend.py" in contents) == (
            profile == "sql"
        )
        assert ("lib/kofin/sync/backends/api/requirements.json" in contents) == (
            profile == "api"
        )


def test_api_package_starts_with_native_imports_and_file_access_forbidden(tmp_path):
    build = _build_module()
    stage = tmp_path / "addon"
    build.stage(stage, "api")
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


def test_api_package_preserves_live_browser_contracts(tmp_path):
    """Run the established listing/field/URL goldens against shipped API code."""
    build = _build_module()
    stage = tmp_path / "addon"
    build.stage(stage, "api")
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


def test_api_manifest_keeps_every_native_content_type_under_the_native_tree(tmp_path):
    import xml.etree.ElementTree as ET

    build = _build_module()
    stage = tmp_path / "addon"
    build.stage(stage, "api")
    addon = ET.parse(stage / "addon.xml").getroot()
    plugin = next(
        ext
        for ext in addon.findall("extension")
        if ext.get("point") == "xbmc.python.pluginsource"
    )
    assert {
        (path.get("content"), path.text)
        for path in plugin.findall("medialibraryscanpath")
    } == {("movies", "/native/"), ("tvshows", "/native/"), ("musicvideos", "/native/")}
