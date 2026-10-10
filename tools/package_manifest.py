"""One installable tree for release ZIPs and development deployment."""

import ast
import os
from pathlib import Path
import xml.etree.ElementTree as ET

# Path components skipped wherever they appear: VCS, virtualenvs, caches.
EXCLUDE_ANYWHERE = {
    "CLAUDE.md",
    # Sits alongside CLAUDE.md rather than in EXCLUDE_TOP because agent config
    # can be directory-scoped, and it holds local settings that must never
    # reach an installed addon. It is gitignored, but the build copies the
    # working tree, so being ignored is not enough on its own.
    ".claude",
    ".codex",
    ".agents",
    ".aws",
    ".ruff_cache",
    ".git",
    ".venv",
    "venv",
    ".tox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
}
# Repo-root entries that are development-only (mirrors dev-install.sh, plus the
# build's own output dir and common editor/CI folders).
EXCLUDE_TOP = {
    "docs",
    "tests",
    "tools",
    "dist",
    "mypy.ini",
    "tox.ini",
    "pyproject.toml",
    "requirements-dev.txt",
    "README.md",
    "CONTRIBUTING.md",
    ".gitignore",
    ".github",
    ".vscode",
    ".idea",
}
# Suffixes never shipped: byte-compiled Python, and the stray SQLite files a
# test run can leave in the repo root. The databases are gitignored for
# exactly that reason, but this build copies the *working tree* — a shipped
# kofin.db is 70-odd KB of somebody's test data riding into every install
# (found in the 0.13.0 zip).
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm")


# Prefixes, relative to the addon root. These files must be absent, not merely
# unreachable behind a setting, in every API distribution.
SQL_ONLY = (
    "lib/kofin/core/addonxml.py",
    "lib/kofin/sync/backends/sql/",
    "lib/kofin/sync/kodidb/",
    "lib/kofin/sync/writers/",
    "lib/kofin/sync/db.py",
    "lib/kofin/sync/schema.py",
    "lib/kofin/sync/clean.py",
    "lib/kofin/sync/widgetstate.py",
    "lib/kofin/sync/refresh.py",
    "lib/kofin/sync/musicsources.py",
    "lib/kofin/sync/nodes/music.py",
    "lib/kofin/service/artcache.py",
    "lib/kofin/service/chapters.py",
    "lib/kofin/downloads/repoint.py",
    "lib/kofin/plugin/clean.py",
    "context_download_playlist.py",
    "context_playlist.py",
)
PROFILES = ("sql", "api")


def selected_profile(root):
    source = ast.parse((root / "lib/kofin/buildconfig.py").read_text())
    for statement in source.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "BACKEND"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if value in PROFILES:
                return value
    raise ValueError("missing explicit build backend")


def allowed(path, profile):
    if profile not in PROFILES:
        raise ValueError("unknown package profile")
    name = path.as_posix()
    if profile == "api":
        return not any(
            name.startswith(p) if p.endswith("/") else name == p for p in SQL_ONLY
        )
    return not name.startswith("lib/kofin/sync/backends/api/")


def iter_files(root, profile):
    """Every repo-relative path to package, in deterministic order."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        at_root = rel == Path(".")
        # Prune (and order) directories in place so os.walk skips them.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in EXCLUDE_ANYWHERE and not (at_root and name in EXCLUDE_TOP)
        )
        for name in sorted(filenames):
            if name in EXCLUDE_ANYWHERE or name.endswith(EXCLUDE_SUFFIX):
                continue
            if at_root and name in EXCLUDE_TOP:
                continue
            path = rel / name
            if allowed(path, profile) and not (root / path).is_symlink():
                yield path


def contents(root, path, profile):
    data = (root / path).read_bytes()
    if path.as_posix() == "lib/kofin/buildconfig.py":
        source = data.decode("utf-8")
        current = selected_profile(root)
        return source.replace(
            'BACKEND = "' + current + '"', 'BACKEND = "' + profile + '"'
        ).encode("utf-8")
    if profile != "api":
        return data
    if path.as_posix() == "addon.xml":
        tree = ET.fromstring(data)
        # Only the committed native tree is scannable in this preview: the
        # interactive root, search and the user-dependent menus never are.
        for extension in tree.findall("extension"):
            for entry in extension.findall("medialibraryscanpath"):
                if entry.get("content") in ("movies", "tvshows", "musicvideos"):
                    entry.text = "/native/"
                else:
                    extension.remove(entry)
        requires = tree.find("requires")
        floor = requires.find("import[@addon='xbmc.addon']")
        if floor is None:
            ET.SubElement(
                requires, "import", {"addon": "xbmc.addon", "version": "21.90.802"}
            )
        for menu in tree.findall(".//menu"):
            for item in list(menu):
                if item.get("library") in (
                    "context_playlist.py",
                    "context_download_playlist.py",
                ):
                    menu.remove(item)
        return ET.tostring(tree, encoding="utf-8", xml_declaration=True)
    if path.as_posix() == "resources/settings.xml":
        tree = ET.fromstring(data)
        disabled = {
            "cleanDatabases",
            "reuseLanguageInvoker",
            "chapterImages",
            "syncDuringPlay",
            "limitIndex",
            "limitThreads",
            "refreshBoxsets",
        }
        for group in tree.findall(".//group"):
            for setting in list(group):
                if setting.get("id") in disabled:
                    group.remove(setting)
        return ET.tostring(tree, encoding="utf-8", xml_declaration=True)
    return data
