"""What the installable tree ships, and what the build leaves out."""

import importlib.util
import os
import xml.etree.ElementTree as ET

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _build_module():
    path = os.path.join(REPO_ROOT, "tools", "build.py")
    spec = importlib.util.spec_from_file_location("kofin_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_excludes_repo_docs():
    names = {path.name for path in _build_module().iter_files() if len(path.parts) == 1}
    assert "README.md" not in names
    assert "CONTRIBUTING.md" not in names
    assert "CLAUDE.md" not in names
    assert "addon.xml" in names
    script = open(
        os.path.join(REPO_ROOT, "tools", "dev-install.sh"), encoding="utf-8"
    ).read()
    assert "--exclude 'README.md'" in script
    assert "--exclude 'CONTRIBUTING.md'" in script


def test_sync_playlists_and_nfo_export_default_on():
    root = ET.parse(os.path.join(REPO_ROOT, "resources", "settings.xml")).getroot()
    defaults = {
        setting.get("id"): (setting.findtext("default") or "")
        for setting in root.iter("setting")
    }
    assert defaults["syncMusicPlaylists"] == "true"
    assert defaults["downloadsExportMetadata"] == "true"


def test_save_to_jellyfin_is_hidden_in_kofin_playlist_folders():
    root = ET.parse(os.path.join(REPO_ROOT, "addon.xml")).getroot()
    visible = ""
    for item in root.iter("item"):
        if item.get("library") == "context_playlist.py":
            visible = item.findtext("visible") or ""
    assert "playlists/music/Kofin/" in visible
    assert "playlists/video/Kofin/" in visible
    assert visible.startswith("[String.EndsWith")
